# -*- coding: utf-8 -*-
"""页面有效性判定 + 番号路径转换的回归测试。

这两个模块是 2026-11-07 抢修的产物，锁死当时实测到的站点行为，
避免以后有人「简化」判断逻辑又把生产打挂。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import page_guard
from javbus_path import build_detail_url, to_javbus_path


# ---------------------------------------------------------------- 页面判定

def test_dead_page_detected():
    """域名停放页：HTTP 200 但内容是售卖页 —— 生产踩过的坑。"""
    parked = (
        "<html><head><title>This domain is for sale</title></head>"
        "<body>Re-visit the page to contact the owner."
        "<form action='/x'>Name Email Message Website Send</form></body></html>"
    )
    assert page_guard.is_dead_page(parked)
    assert not page_guard.is_usable_detail_page(parked)
    assert page_guard.describe_page(parked) == "dead_page"


def test_age_verify_detected():
    """JavBus 自家的年龄验证弹窗。"""
    body = '<div class="modal show" id="ageVerify"><form id="form1">' \
           '<input id="submit" value="確認" type="submit" name="Submit"/></form></div>'
    assert page_guard.is_age_verify_page(body)
    assert not page_guard.is_usable_detail_page(body)
    assert page_guard.describe_page(body) == "age_verify"


def test_challenge_page_detected():
    body = "<html><title>Just a moment...</title><div id='cf-chl-test'></div></html>"
    assert page_guard.is_challenge_page(body)
    assert page_guard.describe_page(body) == "challenge_page"


def test_real_detail_page_is_usable():
    """真详情页带 gid，必须被认成可用。"""
    body = "<html><script>var gid = 12345; var uc = 0;</script><h3>GMA-090</h3></html>"
    assert page_guard.has_gid(body)
    assert page_guard.is_usable_detail_page(body)
    assert page_guard.describe_page(body) == "content"


def test_welcome_shell_is_not_usable():
    """空壳欢迎页（busdmm.art 那种）没有 gid，不能当详情页。"""
    body = "<html><title>welcome</title><body>Loading……</body></html>"
    assert not page_guard.has_gid(body)
    assert not page_guard.is_usable_detail_page(body)


# ---------------------------------------------------------------- 番号转换

def test_code_path_conversion():
    """库内 id → JavBus 路径形态。

    实测（2026-11-07 生产容器内）：``jur00696`` / ``gma00090`` 直接拼 URL
    必然 404，转成 ``JUR-696`` / ``GMA-090`` 才能命中。
    """
    assert to_javbus_path("JUFE-633") == "JUFE-633"
    assert to_javbus_path("jur00696") == "JUR-696"
    assert to_javbus_path("gma00090") == "GMA-090"
    assert to_javbus_path("rbb00340") == "RBB-340"
    assert to_javbus_path("ofje00646") == "OFJE-646"


def test_code_keeps_three_digit_leading_zero():
    """三位编号必须保留前导零：``JUR-097`` 不能变成 ``JUR-97``。"""
    assert to_javbus_path("JUR-097") == "JUR-097"


def test_short_code_is_zero_padded_to_three_digits():
    """短编号要补零到 3 位 —— 这是站点的硬规则。

    2026-11-07 生产容器内实测（20 个短编号）：
        HTTM-60 404 / HTTM-060 200    AA-2 404 / AA-002 200
        SS-15 404 / SS-015 200        MIRD-28 404 / MIRD-028 200
    站点对数字位数是敏感的：多一位也不行（``JUR-0696`` 404，``JUR-696`` 200）。
    """
    assert to_javbus_path("HTTM-60") == "HTTM-060"
    assert to_javbus_path("AA-2") == "AA-002"
    assert to_javbus_path("AAID-01") == "AAID-001"
    assert to_javbus_path("SS-15") == "SS-015"
    assert to_javbus_path("MIRD-28") == "MIRD-028"


def test_amane_filled_code_is_stripped_then_padded_to_three():
    """amane 的 5 位 0 填充：先去掉多余零，再补到 3 位。

    实测 ``GMA-90`` 404 而 ``GMA-090`` 200 —— 去零后必须补回三位。
    """
    assert to_javbus_path("jur00696") == "JUR-696"
    assert to_javbus_path("gma00090") == "GMA-090"
    assert to_javbus_path("rbb00340") == "RBB-340"
    assert to_javbus_path("ofje00646") == "OFJE-646"


def test_unsupported_codes_untouched():
    """站上没有的番号形态原样返回，别猜错形态浪费请求。"""
    assert to_javbus_path("ASBX6559") == "ASBX6559"
    assert to_javbus_path("SS-155EX") == "SS-155EX"
    assert to_javbus_path("NAAC-060B") == "NAAC-060B"
    assert to_javbus_path("021026-100") == "021026-100"


def test_build_detail_url():
    assert build_detail_url("https://www.buscdn.casa", "jur00696") == \
        "https://www.buscdn.casa/JUR-696"
    assert build_detail_url("https://www.buscdn.casa/", "JUFE-633") == \
        "https://www.buscdn.casa/JUFE-633"
    assert build_detail_url("", "JUFE-633") == ""
    assert build_detail_url("https://www.buscdn.casa", "") == ""


# ---------------------------------------------------------------- 详情路径归一化


def test_normalize_detail_path_converts_amane_filled_id():
    """库内 0 填充 id 必须转成 JavBus 形态，否则所有域名一起 404。

    生产实测：force_sync 之类直接传 f"/{movie_id}" 的调用方，
    会把 jur00808 原样拼到 URL 上，13 个候选域名一起 HTTPError ——
    看起来像「域名全挂了」，实际是路径不对。
    """
    from scraper import JavBusScraper

    assert JavBusScraper._normalize_detail_path("/jur00808") == "/JUR-808"
    assert JavBusScraper._normalize_detail_path("/jur00696") == "/JUR-696"
    assert JavBusScraper._normalize_detail_path("/gma00090") == "/GMA-090"


def test_normalize_detail_path_keeps_prefix_and_query():
    from scraper import JavBusScraper

    assert JavBusScraper._normalize_detail_path("/zh/jur00696") == "/zh/JUR-696"
    assert JavBusScraper._normalize_detail_path("/jur00696?x=1") == "/JUR-696?x=1"
    assert JavBusScraper._normalize_detail_path("/JUFE-633") == "/JUFE-633"
    assert JavBusScraper._normalize_detail_path("/") == "/"


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  OK {name}")
    print("all passed")
