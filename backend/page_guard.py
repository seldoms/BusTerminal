# -*- coding: utf-8 -*-
"""站点返回页面的有效性判定。

JavBus 系（javbus / busdmm / busfan / buscdn / cdnbus / dmmsee …）是同一批
镜像，同源站点对「不像浏览器」或「访问过于密集」的请求会返回几种**看起来正常
但根本不是影片页**的响应。2026-11-07 生产实测把这些形态全部踩了一遍：

======================  ======================  ==========================
形态                    HTTP / 特征             后果
======================  ======================  ==========================
年龄验证弹窗            200 + ``id="ageVerify"``  页面里没有 ``var gid``
                                              → 「无法在页面中识别出 gid」
停放页（域名被挂卖）    200 + "for sale"         同上，且**永不变化**
空壳欢迎页              200 + 只有 "welcome"     同上
Cloudflare 人机验证     200 + "just a moment"    同上
驱动验证                302 → ``/doc/driver-verify``
======================  ======================  ==========================

**最阴险的是停放页**：它返回 200 且内容稳定，`record_success` 会被误判成功、
域名评分一路上涨，于是长期霸占候选池第一梯队，每次抓详情都白跑一趟。
2026-11-07 实测 ``www.busdmm.cyou`` 就是这种页面（"This domain is for sale"），
而它当时在候选池里排第 3 位。

因此判定不能只看 HTTP 状态码，必须看**页面内容**。
"""
from typing import Optional

# Cloudflare 系挑战页
_CHALLENGE_MARKERS = (
    "just a moment",
    "cf-chl-",
    "verify you are human",
    "captcha",
)

# 域名停放页 / 空壳页。命中即说明这个域名已经废了（不再是镜像），
# 应当判死并从候选池剔除，而不是继续拿它当「成功」。
_DEAD_PAGE_MARKERS = (
    "this domain is for sale",
    "domain is for sale",
    "buy this domain",
    "sedoparking",
    "parkingcrew",
    "domain parking",
)

# 年龄验证弹窗（JavBus 自家的「你是否已經成年?」）
_AGE_VERIFY_MARKERS = (
    'id="ageVerify"',
    "id='ageVerify'",
    'name="Submit"',
    "id=ageVerify",
)

# 详情页真正需要的脚本变量：磁力链接靠 Ajax，用 gid 换取。
_GID_MARKER = "var gid"


def is_challenge_page(text: str) -> bool:
    """Cloudflare 等人机验证页。"""
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _CHALLENGE_MARKERS)


def is_age_verify_page(text: str) -> bool:
    """JavBus 自家的年龄验证弹窗。"""
    return any(marker in (text or "") for marker in _AGE_VERIFY_MARKERS)


def is_dead_page(text: str) -> bool:
    """域名停放页 / 空壳页 —— 这个域名已经不是镜像了。"""
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _DEAD_PAGE_MARKERS)


def has_gid(text: str) -> bool:
    """页面里有没有磁力所需的 gid 脚本变量。"""
    return _GID_MARKER in (text or "")


def describe_page(text: str) -> str:
    """给日志用的简短页面描述。"""
    if is_age_verify_page(text):
        return "age_verify"
    if is_dead_page(text):
        return "dead_page"
    if is_challenge_page(text):
        return "challenge_page"
    if has_gid(text):
        return "content"
    return "no_gid"


def is_usable_detail_page(text: str) -> bool:
    """这个响应能不能当详情页用。

    判据是**有没有 gid**：详情页一定带它，而年龄验证页 / 停放页 / 空壳页都没有。
    比逐个排除挑战页更稳 —— 站点以后加新的拦截形态也不会漏判。
    """
    if not text:
        return False
    if is_dead_page(text):
        return False
    return has_gid(text)
