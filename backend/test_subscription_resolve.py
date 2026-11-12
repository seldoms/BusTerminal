"""内部女优订阅「解析不出真实来源页」的两层根因（2026-11-04 实测）。

`jellyfin:actress:r18:1059227` 这类订阅要走两步才能抓：
1. 把 subject 换成 `actresses` 表里那一行（subject 是 `source:source_id`，**不是** id）；
2. 拿女优名去站点搜 `/star/` 页。

两层都可能坏：
- 第 1 层原实现一律按 `actresses.id` 查 → 294 条内部订阅里 291 条查不到人 →
  `name` 为空 → 直接返回 None，**从来没真正抓过一次**；
- 第 2 层 javbus 反爬时会把 `/searchstar/`、`/actresses` 甚至 `/star/` 全部 302 到
  `/doc/driver-verify` 并回 **200**，页面里一个 `/star/` 链接都没有 ——
  「HTTP 200 就当成功」的写法会把反爬当成空结果。
"""

from unittest.mock import MagicMock
from urllib.parse import quote

from subscription_service import SubscriptionService


def _build(db=None, scraper=None):
    db = db or MagicMock()
    db.is_movie_downloaded.return_value = False
    db.get_local_file.return_value = None
    # 名字兜底默认查不到：MagicMock 的默认返回值是 truthy，会让「查不到这个女优」
    # 这类用例误判成找到了人（v1.96 新增 `get_actress_by_name` 兜底层后的回归）。
    db.get_actress_by_name.return_value = None
    scraper = scraper or MagicMock()
    service = SubscriptionService(
        db=db,
        scraper=scraper,
        logger=MagicMock(),
        is_internal_subscription_url=lambda url: str(url or "").startswith("jellyfin:"),
        normalize_scraper_url=lambda url: url,
        normalize_magnet_url=lambda url: url,
        queue_actress_metadata_from_detail=lambda *args: None,
        evaluate_download_permission=lambda *args, **kwargs: {"allowed": False},
        queue_download_if_absent=lambda *args, **kwargs: {},
        safe_exception_label=lambda exc: type(exc).__name__,
        display_value_for_log=str,
    )
    return service, db, scraper


def test_subject_with_source_and_id_is_looked_up_by_source():
    """`r18:1059227` 是 source:source_id，不是 actresses.id。"""
    db = MagicMock()
    db.get_actress.return_value = None  # 按 id 查不到（生产实况）
    db.get_actress_by_source.return_value = {
        "id": "r18:1059227-internal",
        "name": "木下凛々子",
        "page_url": "https://www.javbus.com/star/uvy",
    }
    service, _, _ = _build(db=db)

    resolved = service.resolve_actress_to_real_source_url("jellyfin:actress:r18:1059227")

    db.get_actress_by_source.assert_called_once_with("r18", "1059227")
    assert resolved == "https://www.javbus.com/star/uvy"


def test_plain_subject_still_falls_back_to_id_lookup():
    """老格式 `jellyfin:person:某名字`（没有 source 段）仍按 id 查。"""
    db = MagicMock()
    db.get_actress.return_value = {"id": "某名字", "name": "某名字", "page_url": "https://www.javbus.com/star/abc"}
    service, _, _ = _build(db=db)

    assert service.resolve_actress_to_real_source_url("jellyfin:person:某名字") == \
        "https://www.javbus.com/star/abc"


def test_unknown_actress_returns_none_without_touching_site():
    db = MagicMock()
    db.get_actress_by_source.return_value = None
    db.get_actress.return_value = None
    scraper = MagicMock()
    service, _, scraper = _build(db=db, scraper=scraper)

    assert service.resolve_actress_to_real_source_url("jellyfin:actress:r18:9999999") is None
    scraper.session.get.assert_not_called()


def test_search_page_without_star_link_counts_as_failure():
    """javbus 反爬：200 + driver-verify 页，里面没有 /star/ 链接。"""
    db = MagicMock()
    db.get_actress_by_source.return_value = {"id": "x", "name": "木下凛々子", "page_url": None}
    scraper = MagicMock()
    scraper.base_url = "https://www.javbus.com"
    resp = MagicMock()
    resp.status_code = 200
    resp.content = b"<html>driver verify</html>"
    scraper.session.get.return_value = resp
    # parse_html 返回一个查不到 /star/ 的 soup
    scraper.parse_html.return_value = MagicMock(**{"select_one.return_value": None})
    service, _, scraper = _build(db=db, scraper=scraper)

    assert service.resolve_actress_to_real_source_url("jellyfin:actress:r18:1") is None
    assert service._resolve_failure_streak == 1


def test_consecutive_failures_trip_a_cooldown():
    """291 条内部订阅不能逐条白打一个反爬站点。"""
    db = MagicMock()
    db.get_actress_by_source.return_value = {"id": "x", "name": "名字", "page_url": None}
    scraper = MagicMock()
    scraper.base_url = "https://www.javbus.com"
    resp = MagicMock(status_code=200, content=b"x")
    scraper.session.get.return_value = resp
    scraper.parse_html.return_value = MagicMock(**{"select_one.return_value": None})
    service, _, scraper = _build(db=db, scraper=scraper)

    for _ in range(SubscriptionService.RESOLVE_FAILURE_STREAK_LIMIT):
        service.resolve_actress_to_real_source_url("jellyfin:actress:r18:1")

    calls_before = scraper.session.get.call_count
    # 每次解析失败会依次试两个搜索路由（新路由 + 旧路由兜底）
    assert calls_before == SubscriptionService.RESOLVE_FAILURE_STREAK_LIMIT * 2

    # 冷却期内：直接返回 None，一次站都不打
    assert service.resolve_actress_to_real_source_url("jellyfin:actress:r18:2") is None
    assert scraper.session.get.call_count == calls_before


def test_success_clears_the_streak():
    db = MagicMock()
    db.get_actress_by_source.return_value = {"id": "x", "name": "名字", "page_url": None}
    scraper = MagicMock()
    scraper.base_url = "https://www.javbus.com"
    resp = MagicMock(status_code=200, content=b"<a href='/star/uvy'>x</a>")
    scraper.session.get.return_value = resp
    link = MagicMock()
    link.get.return_value = "/star/uvy"
    scraper.parse_html.return_value = MagicMock(**{"select_one.return_value": link})
    service, _, _ = _build(db=db, scraper=scraper)

    service._resolve_failure_streak = 3
    assert service.resolve_actress_to_real_source_url("jellyfin:actress:r18:1") == \
        "https://www.javbus.com/star/uvy"
    assert service._resolve_failure_streak == 0


def test_search_uses_new_searchstar_route_first():
    """站点改了女优搜索路由：/searchstar/<kw> 才是女优搜索。

    2026-11-07 实测：旧路由 /search/actresses?fKeyword=<kw> 现在返回的是
    **影片**列表（结果区是 movie-box 指向 /<番号>），一个 /star/ 链接都没有，
    于是「解析失败」→ 回落 netflav → 一条磁力都拿不到。
    新路由 /searchstar/<kw>（站点 searchs() 函数里拼的）才返回女优页。
    """
    db = MagicMock()
    db.get_actress_by_source.return_value = {"id": "x", "name": "明里", "page_url": None}
    scraper = MagicMock()
    scraper.base_url = "https://www.javbus.com"
    resp = MagicMock(status_code=200, content=b"<a href='/star/qs6'>x</a>")
    scraper.session.get.return_value = resp
    link = MagicMock()
    link.get.return_value = "/star/qs6"
    scraper.parse_html.return_value = MagicMock(**{"select_one.return_value": link})
    service, _, scraper = _build(db=db, scraper=scraper)

    service.resolve_actress_to_real_source_url("jellyfin:actress:r18:1")

    first_url = scraper.session.get.call_args_list[0][0][0]
    assert "/searchstar/" in first_url
    # 关键词是 URL 编码后的（日文名走 quote）
    assert quote("明里") in first_url


def test_search_falls_back_to_legacy_route():
    """新路由拿不到 star 链接时，仍要试旧路由（站点改回去也能工作）。"""
    db = MagicMock()
    db.get_actress_by_source.return_value = {"id": "x", "name": "明里", "page_url": None}
    scraper = MagicMock()
    scraper.base_url = "https://www.javbus.com"

    legacy = MagicMock(status_code=200, content=b"<a href='/star/qs6'>x</a>")
    scraper.session.get.side_effect = [
        MagicMock(status_code=200, content=b"<html>no star here</html>"),
        legacy,
    ]
    link = MagicMock()
    link.get.return_value = "/star/qs6"
    # 第一次解析不到，第二次能解析到
    scraper.parse_html.side_effect = [
        MagicMock(**{"select_one.return_value": None}),
        MagicMock(**{"select_one.return_value": link}),
    ]
    service, _, scraper = _build(db=db, scraper=scraper)

    assert service.resolve_actress_to_real_source_url("jellyfin:actress:r18:1") == \
        "https://www.javbus.com/star/qs6"
    urls = [call[0][0] for call in scraper.session.get.call_args_list]
    assert "/searchstar/" in urls[0]
    assert "/search/actresses?fKeyword=" in urls[1]
