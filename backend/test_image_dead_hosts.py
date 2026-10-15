"""远程图片代理的失效域名与超时策略。

回归背景（订阅页「图片加载慢」）：
逐张展开订阅卡实测，快卡 63~162ms、慢卡 4780~5483ms，
慢的恰好是封面指向 `www.seejav.cyou` 的那些 —— 该域名实测直连 16 秒无响应。
而回源超时当时是 (5.0, 20.0)，读取要给到 20 秒，于是每张这样的首访都要干等。
"""
import pytest

from image_cache_service import DEAD_IMAGE_HOSTS, is_dead_image_host


# ------------------------------------------------------------------ 失效域名识别


@pytest.mark.parametrize(
    "url",
    [
        "https://www.seejav.cyou/pics/thumb/a.jpg",
        "http://seejav.cyou/a.jpg",
        "HTTP://WWW.SEEJAV.CYOU/a.jpg",       # 大小写
        "https://www.javbus.cyou/pics/thumb/a.jpg",
        "https://n1.1024cdn.sx/a.jpg",
    ],
)
def test_dead_hosts_detected(url):
    assert is_dead_image_host(url) is True


@pytest.mark.parametrize(
    "url",
    [
        # 这些是活的图床，**绝不能**误判成失效（99.3% 的封面来自 dmm）
        "https://pics.dmm.co.jp/mono/first/a.jpg",
        "https://awsimgsrc.dmm.co.jp/..",
        "https://www.javbus.com/pics/thumb/a.jpg",
        "https://www.busfan.casa/pics/thumb/a.jpg",
        "https://www.buscdn.casa/pics/a.jpg",
        "https://pics.r18.com/mono/a.jpg",
        "https://i.imgur.com/a.jpg",
        "https://raw.githubusercontent.com/a.jpg",
        # 异常输入不能抛异常
        "not-a-url",
        "",
        None,
        123,
    ],
)
def test_live_hosts_not_flagged(url):
    assert is_dead_image_host(url) is False


def test_dead_host_table_is_sane():
    """失效域名表必须是小写、无 scheme、无路径，否则匹配不到。"""
    for host in DEAD_IMAGE_HOSTS:
        assert host == host.lower(), f"{host} 必须是小写"
        assert "/" not in host, f"{host} 不能带路径"
        assert "://" not in host, f"{host} 不能带协议"


# ------------------------------------------------------------------ 超时策略


def test_image_fetch_timeout_is_bounded():
    """回源读取超时应足够短。

    曾经是 20 秒，失效图床首访要干等十几秒（实测 seejav.cyou 直连 16 秒）。
    断言它在合理区间，既不会让慢源站无限拖住，又不至于误杀慢网络下的正常图。
    """
    import inspect

    import image_cache_service

    src = inspect.getsource(image_cache_service)
    assert "timeout=(5.0, 20.0)" not in src, "旧的 20 秒读取超时还在"
    assert "timeout=(4.0, 6.0)" in src, "新的短超时没有生效"