"""qb_api 单元测试（纯本地，不连真实 qBittorrent）。

覆盖 remove_tags：正常路径、hashes 为空、tags 为空、异常路径。
"""
import requests

import qb_api


class FakeResponse:
    """最小响应桩：只需要 ok 属性。"""

    def __init__(self, ok=True):
        self.ok = ok


class FakeSession:
    """记录 post 调用，按需抛异常。"""

    def __init__(self, ok=True, exc=None):
        self.ok = ok
        self.exc = exc
        self.calls = []

    def post(self, url, data=None, timeout=None):
        self.calls.append({"url": url, "data": data, "timeout": timeout})
        if self.exc is not None:
            raise self.exc
        return FakeResponse(self.ok)


def test_remove_tags_posts_remove_tags_endpoint():
    session = FakeSession()
    assert qb_api.remove_tags(session, "http://qb.local", ["a" * 40, "b" * 40], "Delivered") is True

    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"] == "http://qb.local/api/v2/torrents/removeTags"
    # 与 qB Web API 一致：hashes 用 | 拼接，tags 用逗号拼接
    assert call["data"] == {"hashes": f"{'a' * 40}|{'b' * 40}", "tags": "Delivered"}


def test_remove_tags_returns_false_when_http_not_ok():
    session = FakeSession(ok=False)
    assert qb_api.remove_tags(session, "http://qb.local", ["a" * 40], "Delivered") is False


def test_remove_tags_rejects_empty_hashes():
    session = FakeSession()
    assert qb_api.remove_tags(session, "http://qb.local", [], "Delivered") is False
    assert qb_api.remove_tags(session, "http://qb.local", ["", "  "], "Delivered") is False
    assert qb_api.remove_tags(session, "http://qb.local", None, "Delivered") is False
    assert session.calls == []


def test_remove_tags_rejects_empty_tags():
    session = FakeSession()
    assert qb_api.remove_tags(session, "http://qb.local", ["a" * 40], "") is False
    assert qb_api.remove_tags(session, "http://qb.local", ["a" * 40], "   ") is False
    assert qb_api.remove_tags(session, "http://qb.local", ["a" * 40], None) is False
    assert session.calls == []


def test_remove_tags_returns_false_on_unexpected_exception():
    session = FakeSession(exc=RuntimeError("boom"))
    assert qb_api.remove_tags(session, "http://qb.local", ["a" * 40], "Delivered") is False


def test_remove_tags_reraises_network_exception():
    # 与 add_tags 保持一致：网络类异常交给上层熔断处理，不在这里吞掉
    session = FakeSession(exc=requests.exceptions.ConnectionError("unreachable"))
    try:
        qb_api.remove_tags(session, "http://qb.local", ["a" * 40], "Delivered")
    except requests.exceptions.ConnectionError:
        return
    raise AssertionError("ConnectionError should be re-raised")
