"""qB 主机地址与端口的解析规则。

回归背景（两处真实故障）：
1. 前端表单读写 `qbConfig.url`，后端 `QBConfig` 要 `host`+`port` → 必 422。
2. `normalize_host` 早期拒绝「地址里带端口」，而界面上标签是「服务器 API 地址」，
   用户填 `http://192.168.1.5:8080`（最自然写法）必然保存失败。
"""
import pytest

from qb_service import QBService


class _Svc:
    """只借用两个纯函数，不需要真实构造服务。"""

    normalize_host = QBService.normalize_host
    split_host_port = QBService.split_host_port


@pytest.fixture
def svc():
    return _Svc()


# ------------------------------------------------------------------ 正常拆分


@pytest.mark.parametrize(
    "raw,fallback_port,expected_host,expected_port",
    [
        # 回归：地址里带端口（用户最自然的写法）必须被接受，端口拆出来
        ("http://192.168.1.100:8086", 8080, "http://192.168.1.100", 8086),
        ("http://192.168.1.5:8080/", 1, "http://192.168.1.5", 8080),
        ("192.168.1.5:8080", 1, "192.168.1.5", 8080),
        # 不带端口时用表单里的端口字段
        ("https://qb.example.com", 16601, "https://qb.example.com", 16601),
        ("192.168.1.5", 8080, "192.168.1.5", 8080),
        # 地址里的端口优先于表单端口
        ("http://h:1", 9999, "http://h", 1),
    ],
)
def test_split_host_port(svc, raw, fallback_port, expected_host, expected_port):
    assert svc.split_host_port(raw, fallback_port) == (expected_host, expected_port)


# ------------------------------------------------------------------ 必须拒绝


@pytest.mark.parametrize(
    "raw,expected_error",
    [
        # 端口段存在但不是合法端口号 —— 早期版本被先行剥掉，
        # `http://h:abc` 会悄悄变成 host=http://h 且端口用回表单值，看着"保存成功"但地址是错的
        ("http://h:abc", "invalid_qb_port"),
        ("http://h:99999", "invalid_qb_port"),
        ("http://h:0", "invalid_qb_port"),
        ("h:abc", "invalid_qb_port"),
        # 地址里带凭据 / 路径 —— 不允许
        ("http://user:pass@h:8080", "invalid_qb_host"),
        ("http://h/x", "invalid_qb_host"),
        ("", "invalid_qb_host"),
        ("   ", "invalid_qb_host"),
        (None, "invalid_qb_host"),
        # 含空格
        ("http://h ost", "invalid_qb_host"),
    ],
)
def test_split_host_port_rejects(svc, raw, expected_error):
    with pytest.raises(ValueError) as exc:
        svc.split_host_port(raw, 8080)
    assert str(exc.value) == expected_error


# ------------------------------------------------------------------ normalize_host 本身


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://qb.example.com", "https://qb.example.com"),
        ("http://192.168.1.5:8080/", "http://192.168.1.5"),
        ("  http://h:8080  ", "http://h"),
        # 非法端口会让 normalize_host 直接拒绝（由 split_host_port 转成 invalid_qb_port）
        ("http://h:abc", None),
        ("http://user@h", None),
    ],
)
def test_normalize_host(svc, raw, expected):
    assert svc.normalize_host(raw) == expected