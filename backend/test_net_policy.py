"""net_policy 代理开关测试。

覆盖：默认值（生产安全）、环境变量、库配置优先级、以及接入点确实生效。
"""
import pytest
import requests

import config
import net_policy


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """每个用例都从「库里没配」开始，避免相互污染。"""
    monkeypatch.setattr(net_policy, "_db_value", lambda: None)
    monkeypatch.setattr(config, "INTERNAL_PROXY_ENABLED", False, raising=False)
    yield


def test_default_disabled():
    """生产默认关闭：内网调用不能被 IDE 注入的代理劫持。"""
    assert net_policy.internal_proxy_enabled() is False


@pytest.mark.parametrize("raw,expected", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
    ("0", False), ("false", False), ("", False), (None, False),
])
def test_as_bool(raw, expected):
    assert net_policy._as_bool(raw) is expected


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("True", True), ("1", True), ("false", False), ("off", False),
])
def test_env_value(monkeypatch, raw, expected):
    monkeypatch.setattr(config, "INTERNAL_PROXY_ENABLED", net_policy._as_bool(raw), raising=False)
    assert net_policy.internal_proxy_enabled() is expected


def test_db_value_wins_over_config(monkeypatch):
    """库里配了就用库里的，DB > env(config)。"""
    monkeypatch.setattr(net_policy, "_db_value", lambda: "false")
    monkeypatch.setattr(config, "INTERNAL_PROXY_ENABLED", True, raising=False)
    assert net_policy.internal_proxy_enabled() is False


def test_db_value_true(monkeypatch):
    monkeypatch.setattr(net_policy, "_db_value", lambda: "true")
    assert net_policy.internal_proxy_enabled() is True


def test_apply_policy_sets_trust_env(monkeypatch):
    session = requests.Session()
    monkeypatch.setattr(net_policy, "_db_value", lambda: "false")
    net_policy.apply_policy(session)
    assert session.trust_env is False

    monkeypatch.setattr(net_policy, "_db_value", lambda: "true")
    net_policy.apply_policy(session)
    assert session.trust_env is True


def test_internal_session_refreshes_each_call(monkeypatch):
    """共享 session 每次都按当前开关刷新，改开关不必重启进程。"""
    monkeypatch.setattr(net_policy, "_db_value", lambda: "false")
    session_off = net_policy.internal_session()
    assert session_off.trust_env is False
    monkeypatch.setattr(net_policy, "_db_value", lambda: "true")
    session_on = net_policy.internal_session()
    assert session_on is session_off  # 同一个对象
    assert session_on.trust_env is True


def test_amane_client_applies_policy(monkeypatch):
    from amane_client import AmaneClient

    monkeypatch.setattr(net_policy, "_db_value", lambda: "false")
    client_a = AmaneClient(base_url="http://127.0.0.1:8000", token="t")
    assert client_a._session.trust_env is False

    monkeypatch.setattr(net_policy, "_db_value", lambda: "true")
    client_b = AmaneClient(base_url="http://127.0.0.1:8000", token="t")
    assert client_b._session.trust_env is True


def test_amane_client_keeps_injected_session_untouched(monkeypatch):
    """测试注入的假 session 不能被改写了，否则 mock 会被策略覆盖。"""
    from amane_client import AmaneClient

    monkeypatch.setattr(net_policy, "_db_value", lambda: "true")
    fake = requests.Session()
    fake.trust_env = False
    client = AmaneClient(base_url="http://127.0.0.1:8000", token="t", session=fake)
    assert client._session is fake
    assert client._session.trust_env is False
