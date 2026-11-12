"""netflav 作为订阅「发现」源：番号归一化、域名轮询、按女优拉片单、订阅回落。

背景（2026-11-04 实测）：javbus 把 `/searchstar/`、`/actresses`、`/star/` 全部
302 到 `/doc/driver-verify` 并回 200，294 条内部女优订阅一条都解析不出来源页。
netflav 的 `/all?actress=<名字>` 仍正常（200 / 8~30 条目），所以用它兜住「发现」，
magnet 仍回 javbus 详情页取。
"""

import logging
from unittest.mock import MagicMock

import pytest

from domain_router import DomainRouter
from netflav_scraper import NetflavScraper
from subscription_service import SubscriptionService


class FakeDb:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.cache = {}

    def get_config(self, key):
        return self.values.get(key)

    def set_config(self, key, value):
        self.values[key] = value

    def get_cache_full(self, key):
        return self.cache.get(key)

    def set_cache(self, key, value):
        self.cache[key] = {"value": value, "updated_at": 0}


# MARK: - 域名路由：备用域名与自动下线


def test_domain_router_disables_dead_domain_after_threshold():
    router = DomainRouter(
        db=FakeDb(),
        source_key="javbus",
        default_domains=["https://dead.example", "https://alive.example"],
        logger=logging.getLogger(__name__),
        failure_threshold=2,
        cooldown_seconds=30,
        dead_threshold=4,
    )
    for _ in range(4):
        router.record_failure("https://dead.example", "SSLError")

    snapshot = router.snapshot()
    dead = next(item for item in snapshot["domains"] if item["domain"] == "https://dead.example")
    assert dead["enabled"] is False
    assert router.candidates() == ["https://alive.example"]


def test_domain_router_never_returns_empty_when_all_domains_disabled():
    """全部域名都被判死时必须退回全部候选，否则抓取链路静默停摆。"""
    router = DomainRouter(
        db=FakeDb(),
        source_key="javbus",
        default_domains=["https://a.example", "https://b.example"],
        logger=logging.getLogger(__name__),
        failure_threshold=2,
        cooldown_seconds=30,
        dead_threshold=3,
    )
    for domain in ("https://a.example", "https://b.example"):
        for _ in range(3):
            router.record_failure(domain, "SSLError")

    assert len(router.candidates()) == 2


# MARK: - netflav 番号与片单


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("MIMK-284", "MIMK-284"),
        ("mimk284", "MIMK-284"),
        ("MIMK_284", "MIMK-284"),
        # 前导零必须保留：库里是 JUR-097，拼成 JUR-97 去抓详情会 404
        ("JUR-097", "JUR-097"),
        ("JUR-97", "JUR-97"),
        ("ABC-00123", "ABC-00123"),
        ("SS155EX", "SS-155EX"),
        ("", None),
        (None, None),
    ],
)
def test_standard_code(raw, expected):
    assert NetflavScraper.standard_code(raw) == expected


def _build_netflav(state_payload, fetch=None):
    scraper = NetflavScraper(domain_provider=lambda: [])
    scraper._fetch_html = fetch or MagicMock(return_value=state_payload)
    return scraper


def test_list_movies_for_actress_builds_subscription_items():
    state = """<!doctype html><script id="__NEXT_DATA__" type="application/json">
    {"props":{"initialState":{"all":{"docs":[
      {"code":"MIMK-284","title":"作品B","sourceDate":"2026-06-12T00:00:00Z","preview":"http://c/b.jpg","actors":["木下凛々子"]},
      {"code":"ABP-123","title":"作品A","sourceDate":"2026-07-01T00:00:00Z","preview":"http://c/a.jpg","actors":["木下凛々子"]},
      {"title":"没有番号","sourceDate":"2026-07-02T00:00:00Z"}
    ]}}}}
    </script>"""

    scraper = NetflavScraper(domain_provider=lambda: [])
    scraper._get_cached = lambda key, ttl: None  # 跳过缓存
    scraper._fetch_html = MagicMock(return_value=state)

    movies = scraper.list_movies_for_actress("木下凛々子", limit=30)

    assert [movie["id"] for movie in movies] == ["ABP-123", "MIMK-284"]  # 新片在前
    assert movies[0]["date"] == "2026-07-01"
    assert movies[0]["detail_url"] is None  # 由订阅层改写成 javbus 详情
    assert movies[0]["source"] == "netflav"


def test_fetch_html_switches_to_mirror():
    scraper = NetflavScraper(domain_provider=lambda: ["https://mirror.example"])
    # 用干净的 router，避免其它用例在共享 config 里留下的健康分影响优先级
    scraper.domain_router = DomainRouter(
        db=FakeDb(),
        source_key="netflav",
        default_domains=["https://netflav.com", "https://mirror.example"],
        logger=logging.getLogger(__name__),
    )
    seen = []

    def fake_get(url, timeout=20):
        seen.append(url)
        if "netflav.com" in url:
            raise RuntimeError("ssl_eof")
        resp = MagicMock(status_code=200, text="<html>ok</html>")
        return resp

    scraper.session.get = fake_get
    # 缩短退避，测试不真等
    import netflav_scraper as module
    original = module.time.sleep
    module.time.sleep = lambda _s: None
    try:
        html = scraper._fetch_html("/actress", retries=1)
    finally:
        module.time.sleep = original

    assert html == "<html>ok</html>"
    assert any("netflav.com" in url for url in seen)
    assert any("mirror.example" in url for url in seen)


# MARK: - 订阅回落


def _build_subscription(db=None, scraper=None, netflav=None):
    db = db or MagicMock()
    db.is_movie_downloaded.return_value = False
    db.get_local_file.return_value = None
    # 默认查不到：MagicMock 的默认返回值 truthy，会让「名字兜底」误命中
    db.get_actress_by_name.return_value = None
    scraper = scraper or MagicMock()
    scraper.base_url = "https://www.javbus.com"
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
        netflav_scraper=netflav,
    )
    return service, db, scraper


def test_resolve_falls_back_to_netflav_when_javbus_page_missing():
    db = MagicMock()
    db.get_actress_by_source.return_value = {"id": "r18:1", "name": "木下凛々子", "page_url": None}
    scraper = MagicMock()
    scraper.base_url = ""  # 无可用域名 → javbus 侧解析直接失败
    netflav = MagicMock()
    service, _, _ = _build_subscription(db=db, scraper=scraper, netflav=netflav)

    target = service.resolve_subscription_target("jellyfin:actress:r18:1")

    assert target == {"kind": "netflav", "name": "木下凛々子"}


def test_resolve_prefers_javbus_when_page_url_known():
    db = MagicMock()
    db.get_actress_by_source.return_value = {
        "id": "r18:1",
        "name": "木下凛々子",
        "page_url": "https://www.javbus.com/star/uvy",
    }
    netflav = MagicMock()
    service, _, _ = _build_subscription(db=db, netflav=netflav)

    target = service.resolve_subscription_target("jellyfin:actress:r18:1")

    assert target["kind"] == "javbus"
    assert target["url"] == "https://www.javbus.com/star/uvy"


def test_process_subscription_uses_netflav_and_rewrites_detail_url():
    db = MagicMock()
    db.get_actress_by_source.return_value = {"id": "r18:1", "name": "木下凛々子", "page_url": None}
    db.is_movie_downloaded.return_value = False
    db.get_local_file.return_value = None
    db.get_active_queue_entry.return_value = None
    scraper = MagicMock()
    scraper.base_url = "https://www.javbus.com"
    scraper.select_best_magnet.return_value = {"magnet": "magnet:?xt=urn:btih:abc"}
    scraper.get_movie_detail.return_value = {
        "magnets": [{"magnet": "magnet:?xt=urn:btih:abc"}],
        "genres": [],
    }
    scraper.get_movies.return_value = []

    netflav = MagicMock()
    netflav.list_movies_for_actress.return_value = [
        {"id": "MIMK-284", "title": "作品B", "original_title": "作品B", "date": "2026-06-12", "detail_url": None},
    ]
    service, _, scraper = _build_subscription(db=db, scraper=scraper, netflav=netflav)

    report = service.process_subscription(
        {"url": "jellyfin:actress:r18:1", "name": "木下凛々子"}
    )

    assert report["discovery_source"] == "netflav"
    assert report["total_checked"] == 1
    # 详情页回落到 javbus 域名上（magnet 只有它给）
    detail_url = scraper.get_movie_detail.call_args[0][0]
    assert detail_url.endswith("/MIMK-284")
    netflav.list_movies_for_actress.assert_called_once()


def test_legacy_actor_subject_falls_back_to_name_lookup():
    """`jellyfin:actor:<名字>` 的 subject 是名字，库里那行的 id 是 `jf_actor:<名字>`。

    不按名字兜一层的话永远解析不出女优，也就走不到 netflav 回落
    （2026-11-05 生产实测：294 条内部订阅里有这类老格式）。
    """
    db = MagicMock()
    db.get_actress.return_value = None
    scraper = MagicMock()
    scraper.base_url = "https://www.javbus.com"
    scraper.session.get.return_value = MagicMock(status_code=200, content=b"x")
    scraper.parse_html.return_value = MagicMock(**{"select_one.return_value": None})
    service, _, _ = _build_subscription(db=db, scraper=scraper, netflav=MagicMock())
    db.get_actress_by_name.return_value = {"id": "jf_actor:木下莉莉子", "name": "木下莉莉子", "page_url": None}

    target = service.resolve_subscription_target("jellyfin:actor:木下莉莉子")

    db.get_actress_by_name.assert_called_once_with("木下莉莉子")
    assert target == {"kind": "netflav", "name": "木下莉莉子"}


def test_netflav_discovery_normalises_code_to_library_id():
    """netflav 给 `JUR-97`、库里是 `JUR-097`：拼详情页前必须先归一到库内 id。"""
    db = MagicMock()
    db.get_actress_by_source.return_value = {"id": "r18:1", "name": "木下凛々子", "page_url": None}
    db.is_movie_downloaded.return_value = False
    db.get_local_file.return_value = None
    db.get_movie_id_by_code_norm.return_value = "JUR-097"

    scraper = MagicMock()
    scraper.base_url = "https://www.javbus.com"
    scraper.select_best_magnet.return_value = {"magnet": "magnet:?xt=urn:btih:abc"}
    scraper.get_movie_detail.return_value = {"magnets": [{"magnet": "magnet:?xt=urn:btih:abc"}], "genres": []}
    scraper.get_movies.return_value = []

    netflav = MagicMock()
    netflav.list_movies_for_actress.return_value = [
        {"id": "JUR-97", "title": "某标题", "date": "2026-06-12", "detail_url": None},
    ]
    service, _, scraper = _build_subscription(db=db, scraper=scraper, netflav=netflav)

    service.process_subscription({"url": "jellyfin:actress:r18:1", "name": "木下凛々子"})

    assert scraper.get_movie_detail.call_args[0][0].endswith("/JUR-097")


# MARK: - 订阅级异常要带可定位的信息


def test_subscription_exception_carries_message_not_just_type():
    """订阅处理抛异常时，日志/报告里要能看到**消息**，光有类名定位不了。

    2026-11-07 实测：`auto_crawl_subscriptions` 的 failed_count=3 一直查不出
    是哪 3 条、什么原因 —— `safe_exception_label` 只返回异常类名。
    """
    db = MagicMock()
    db.get_actress_by_source.return_value = {"id": "r18:1", "name": "木下凛々子", "page_url": None}
    scraper = MagicMock()
    scraper.base_url = "https://www.javbus.com"

    service, _, _ = _build_subscription(db=db, scraper=scraper, netflav=MagicMock())
    # 来源页解析直接抛异常（模拟「所有域名都挂了」这类真故障）
    service.resolve_subscription_target = MagicMock(
        side_effect=RuntimeError("all_source_domains_failed")
    )

    report = service.process_subscription(
        {"url": "jellyfin:actress:r18:1", "name": "木下凛々子"}
    )

    errors = [item for item in report["logs"] if item["status"] == "error"]
    assert errors, "应记录一条 error"
    reason = errors[0]["reason"]
    assert "RuntimeError" in reason
    assert "all_source_domains_failed" in reason


def test_subscription_exception_reason_is_trimmed_and_deduped():
    """`RuntimeError: RuntimeError: xxx` 这种重复前缀要去掉。"""
    db = MagicMock()
    db.get_actress_by_source.return_value = {"id": "r18:1", "name": "木下凛々子", "page_url": None}
    scraper = MagicMock()
    scraper.base_url = "https://www.javbus.com"

    service, _, _ = _build_subscription(db=db, scraper=scraper, netflav=MagicMock())
    service.resolve_subscription_target = MagicMock(
        side_effect=RuntimeError("RuntimeError: boom")
    )

    report = service.process_subscription(
        {"url": "jellyfin:actress:r18:1", "name": "木下凛々子"}
    )

    errors = [item for item in report["logs"] if item["status"] == "error"]
    assert errors[0]["reason"] == "RuntimeError: boom"


def test_subscription_exception_reason_redacts_secrets():
    """异常消息里的密钥不能进日志。"""
    db = MagicMock()
    db.get_actress_by_source.return_value = {"id": "r18:1", "name": "木下凛々子", "page_url": None}
    scraper = MagicMock()
    scraper.base_url = "https://www.javbus.com"

    service, _, _ = _build_subscription(db=db, scraper=scraper, netflav=MagicMock())
    service.resolve_subscription_target = MagicMock(
        side_effect=RuntimeError("api_key=SUPERSECRET failed")
    )

    report = service.process_subscription(
        {"url": "jellyfin:actress:r18:1", "name": "木下凛々子"}
    )

    errors = [item for item in report["logs"] if item["status"] == "error"]
    assert "SUPERSECRET" not in errors[0]["reason"]


# MARK: - netflav 外部订阅（库里直接存 netflav 演员页 URL 的那几条）


def test_netflav_actress_name_is_extracted_from_page_url():
    """`https://netflav.com/all?actress=<URL 编码的日文名>` 要能取出名字。"""
    scraper = NetflavScraper(domain_provider=lambda: [])

    name = scraper.actress_name_from_url(
        "https://netflav.com/all?actress=%E5%A4%A7%E5%B3%B6%E5%84%AA%E9%A6%99"
    )

    assert name == "大島優香"


def test_actress_name_is_none_for_foreign_host_or_missing_param():
    """非 netflav 域名、或没带 actress 参数，都不该被认成 netflav 演员页。"""
    scraper = NetflavScraper(domain_provider=lambda: [])

    assert scraper.actress_name_from_url("https://www.javbus.com/star/uvy") is None
    assert scraper.actress_name_from_url("https://netflav.com/all") is None
    assert scraper.actress_name_from_url("https://netflav.com/video?id=123") is None


def test_process_subscription_routes_netflav_page_url_to_netflav_discovery():
    """订阅源就是 netflav 演员页时，走 netflav 发现，不当成 JavBus 页面去抓。

    2026-11-07 生产实测：库里 3 条订阅的 url 形如
    `https://netflav.com/all?actress=…`，netflav 不在 JavBus 的 SSRF 白名单里 →
    每轮判 `invalid_subscription_url` → `auto_crawl_subscriptions` 的
    failed_count 恒为 3（与这 3 条数量完全吻合）。
    """
    db = MagicMock()
    db.is_movie_downloaded.return_value = False
    db.get_local_file.return_value = None
    db.get_active_queue_entry.return_value = None
    db.get_movie_id_by_code_norm.return_value = None

    scraper = MagicMock()
    scraper.base_url = "https://www.javbus.com"
    scraper.select_best_magnet.return_value = {"magnet": "magnet:?xt=urn:btih:abc"}
    scraper.get_movie_detail.return_value = {
        "magnets": [{"magnet": "magnet:?xt=urn:btih:abc"}],
        "genres": [],
    }

    netflav = MagicMock()
    netflav.actress_name_from_url.return_value = "大島優香"
    netflav.list_movies_for_actress.return_value = [
        {"id": "MIMK-284", "title": "作品B", "date": "2026-06-12", "detail_url": None},
    ]
    service, _, scraper = _build_subscription(db=db, scraper=scraper, netflav=netflav)

    report = service.process_subscription(
        {
            "url": "https://netflav.com/all?actress=%E5%A4%A7%E5%B3%B6%E5%84%AA%E9%A6%99",
            "name": "大島優香",
        }
    )

    assert report["discovery_source"] == "netflav"
    assert report["total_checked"] == 1
    # JavBus 列表页压根不该被请求（那个域名过不了白名单）
    scraper.get_movies.assert_not_called()
    netflav.list_movies_for_actress.assert_called_once()
    assert netflav.list_movies_for_actress.call_args[0][0] == "大島優香"


def test_netflav_subscription_url_is_accepted_by_url_policy():
    """netflav 演员页 URL 要能过订阅来源校验（否则连删除/刷新接口都用不了）。"""
    from scraper_url_utils import normalize_netflav_subscription_url

    hosts = NetflavScraper(domain_provider=lambda: []).allowed_hosts()
    url = "https://netflav.com/all?actress=%E5%A4%A7%E5%B3%B6%E5%84%AA%E9%A6%99"

    assert normalize_netflav_subscription_url(url, allowed_hosts=hosts) == url
    # 非 netflav 域名不放行
    assert normalize_netflav_subscription_url(
        "https://evil.example/all?actress=x", allowed_hosts=hosts
    ) is None
    # 非 /all 路径不放行
    assert normalize_netflav_subscription_url(
        "https://netflav.com/video?id=1", allowed_hosts=hosts
    ) is None
    # 没有 actress 参数不放行
    assert normalize_netflav_subscription_url(
        "https://netflav.com/all", allowed_hosts=hosts
    ) is None


def test_netflav_host_stays_out_of_generic_scraper_whitelist():
    """通用抓取白名单**不能**放行 netflav —— 只有订阅来源层单独放行。

    `normalize_scraper_url` 是 SSRF 闸门，也是影片详情/下载链路用的。
    netflav 详情页没有 magnet，把它放进通用白名单等于开了个打不通的下载口子。
    """
    from scraper_url_utils import normalize_scraper_url

    assert normalize_scraper_url(
        "https://netflav.com/all?actress=x",
        base_url="https://www.javbus.com",
        all_domains=["https://www.javbus.com"],
    ) is None
    # JavBus 侧照常放行
    assert normalize_scraper_url(
        "https://www.javbus.com/star/uvy",
        base_url="https://www.javbus.com",
        all_domains=["https://www.javbus.com"],
    ) == "https://www.javbus.com/star/uvy"
