"""抓取节奏控制：首页/榜单每天 10 次配额 + 订阅流程 5 分钟节流。

两类抓取是分开的（这是用户 2026-11-03 给的原始口径）：
- **首页/排行榜刷新**：每站每天 10 次，超了只供缓存，不再回源；
- **订阅流程**（列表检索 + 详情抓取）：不占上面那 10 次，改按 5 分钟 + 0~90 秒
  随机抖动排队。

⚠️ 2026-11-04 的教训：一度把订阅检索也塞进「每站每天 10 次」里，结果 308 条订阅
（294 条是 `jellyfin:person:` 内部订阅）要 59 天才能轮一遍，整轮订阅连续几天以
`subscription_sources_unavailable` 收尾 —— 功能等于停摆。5 分钟节流的物理上限
本来就是 288 次/天，正好够一天轮一遍。
"""

import threading
import time
from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

from discovery_service import DiscoveryService
from scrape_budget import (
    PURPOSE_PRELOAD,
    SUBSCRIPTION_LIST_THROTTLE_KEY,
    DetailThrottle,
    ScrapeBudget,
    daily_limit,
    limit_for,
    source_for_url,
)
from subscription_service import SubscriptionService


class _FakeDb:
    """只实现 config 读写，够 ScrapeBudget / DetailThrottle 用。"""

    def __init__(self):
        self.config = {}

    def get_config(self, key):
        return self.config.get(key)

    def set_config(self, key, value):
        self.config[key] = value


class _FakeStopEvent:
    def __init__(self):
        self.waits = []

    def wait(self, timeout):
        self.waits.append(timeout)
        return False


def _build_discovery(budget=None):
    db = MagicMock()
    db.get_translations_batch.return_value = {}
    db.get_cache_full.return_value = {
        "value": [{"id": "cached-1", "title": "缓存片"}],
        "updated_at": time.time(),
    }
    service = DiscoveryService(
        db=db,
        scraper=MagicMock(),
        jable_scraper=MagicMock(),
        discovery_scraper=MagicMock(),
        config=MagicMock(),
        logger=MagicMock(),
        stop_event=threading.Event(),
        cache_ttl_seconds=1800,
        safe_number=float,
        safe_exception_label=lambda exc: type(exc).__name__,
        display_value_for_log=str,
        normalize_magnet_url=lambda value: value,
        evaluate_download_permission=lambda *args, **kwargs: {"allowed": False},
        queue_download_if_absent=lambda *args, **kwargs: {},
        schedule_image_cache_warmup=lambda *args: None,
        scrape_budget=budget,
    )
    return service, db, service.scrape_budget


def _build_subscription(budget=None, list_throttle=None):
    db = MagicMock()
    db.is_movie_downloaded.return_value = False
    db.get_local_file.return_value = None
    scraper = MagicMock()
    scraper.get_movies.return_value = []
    service = SubscriptionService(
        db=db,
        scraper=scraper,
        logger=MagicMock(),
        is_internal_subscription_url=lambda url: False,
        normalize_scraper_url=lambda url: url,
        normalize_magnet_url=lambda url: url,
        queue_actress_metadata_from_detail=lambda *args: None,
        evaluate_download_permission=lambda *args, **kwargs: {"allowed": False},
        queue_download_if_absent=lambda *args, **kwargs: {},
        safe_exception_label=lambda exc: type(exc).__name__,
        display_value_for_log=str,
        scrape_budget=budget,
        list_throttle=list_throttle,
    )
    return service, db, scraper


def test_budget_allows_daily_limit_then_blocks():
    budget = ScrapeBudget(_FakeDb(), MagicMock())
    limit = limit_for(PURPOSE_PRELOAD)
    # 2026-11-05：首页/榜单改成 4 小时一轮后，javbus 每轮 4 次 × 6 轮 = 24 次，
    # 配额 10 会在第 3 轮就烧光、后面的 4 小时节奏形同虚设，所以默认值提到 30。
    assert limit == daily_limit() == 30, "配额必须覆盖 4 小时节奏（24 次/天）并留余量"

    for _ in range(limit):
        assert budget.try_acquire("jable", PURPOSE_PRELOAD) is True

    assert budget.try_acquire("jable", PURPOSE_PRELOAD) is False
    assert budget.used("jable", PURPOSE_PRELOAD) == limit
    assert budget.remaining("jable") == 0


def test_budget_is_per_source():
    budget = ScrapeBudget(_FakeDb(), MagicMock())
    for _ in range(limit_for(PURPOSE_PRELOAD)):
        budget.try_acquire("jable", PURPOSE_PRELOAD)

    assert budget.try_acquire("jable") is False
    assert budget.try_acquire("javbus") is True


def test_budget_resets_next_day(monkeypatch):
    budget = ScrapeBudget(_FakeDb(), MagicMock())
    for _ in range(limit_for(PURPOSE_PRELOAD)):
        budget.try_acquire("jable", PURPOSE_PRELOAD)
    assert budget.try_acquire("jable") is False

    tomorrow = date.today() + timedelta(days=1)

    class _NextDay(date):
        @classmethod
        def today(cls):
            return tomorrow

    monkeypatch.setattr("scrape_budget.date", _NextDay)
    assert budget.try_acquire("jable") is True


def test_budget_persists_across_instances():
    db = _FakeDb()
    first = ScrapeBudget(db, MagicMock())
    for _ in range(limit_for(PURPOSE_PRELOAD)):
        first.try_acquire("javdb", PURPOSE_PRELOAD)

    # 进程重启后从这个 db 再建一个，额度不能复原。
    second = ScrapeBudget(db, MagicMock())
    assert second.try_acquire("javdb") is False


def test_source_for_url_maps_sites():
    assert source_for_url("https://jable.tv/videos/abc/") == "jable"
    assert source_for_url("https://www.javbus.com/ABC-123") == "javbus"
    assert source_for_url("https://javdb.com/v/abc") == "javdb"
    assert source_for_url("https://airav.io/c/abc") == "airav"
    assert source_for_url("https://netflav.com/all?actress=x") == "netflav"
    assert source_for_url("/genre/sub") == "other"


def test_source_for_url_maps_official_mirrors_to_same_bucket():
    """站点官方公告的防屏蔽域名必须归到同一个桶，否则同一站被拆成多份额度。"""
    assert source_for_url("https://www.busdmm.casa/ABC-123") == "javbus"
    assert source_for_url("https://www.dmmsee.casa/") == "javbus"
    assert source_for_url("https://www.seedmm.casa/") == "javbus"
    assert source_for_url("https://www.busfan.cyou/") == "javbus"
    assert source_for_url("https://www.seejav.bond/") == "javbus"


def test_preload_skips_remote_when_budget_exhausted(monkeypatch):
    monkeypatch.setattr("discovery_service.time.sleep", lambda _seconds: None)
    budget = ScrapeBudget(_FakeDb(), MagicMock())
    for _ in range(limit_for(PURPOSE_PRELOAD)):
        budget.try_acquire("javbus", PURPOSE_PRELOAD)
    service, db, _ = _build_discovery(budget=budget)
    db.get_cache_full.return_value = {"value": [{"id": "old"}], "updated_at": 0}

    service.preload_discovery_cache(max_pages=1, force_refresh=True)

    # javbus 额度已满：即使缓存过期也不回源，页面继续读旧缓存。
    service.discovery_scraper.get_javbus_hot.assert_not_called()


def test_preload_charges_budget_per_page(monkeypatch):
    monkeypatch.setattr("discovery_service.time.sleep", lambda _seconds: None)
    budget = ScrapeBudget(_FakeDb(), MagicMock())
    service, db, _ = _build_discovery(budget=budget)
    db.get_cache_full.return_value = None
    service.discovery_scraper.get_javbus_hot.return_value = []
    service.jable_scraper.get_hot_movies.return_value = []
    service.discovery_scraper.get_javdb_hot.return_value = []
    service.discovery_scraper.get_airav_hot.return_value = []

    service.preload_discovery_cache(max_pages=1)

    # 每轮：javbus 1 页、airav 1 页、jable 与 javdb 各 daily/weekly/monthly 三种。
    assert budget.used("javbus") == 1
    assert budget.used("airav") == 1
    assert budget.used("jable") == 3
    assert budget.used("javdb") == 3


def test_subscription_search_does_not_touch_daily_budget(monkeypatch):
    """2026-11-04 实况回归：308 条订阅被塞进「每站每天 10 次」里，59 天才轮一遍。"""
    budget = ScrapeBudget(_FakeDb(), MagicMock())
    for _ in range(limit_for(PURPOSE_PRELOAD)):
        budget.try_acquire("javbus", PURPOSE_PRELOAD)
    assert budget.try_acquire("javbus", PURPOSE_PRELOAD) is False

    throttle = MagicMock()
    service, _, scraper = _build_subscription(budget=budget, list_throttle=throttle)
    scraper.get_movies.return_value = []

    report = service.process_subscription({"name": "订阅A", "url": "https://www.javbus.com/star/uvy"})

    # 配额满了也照查 —— 订阅只受节流管
    scraper.get_movies.assert_called_once()
    throttle.wait.assert_called_once()
    assert report["logs"] == []
    # 而且没有从配额里扣任何东西
    assert budget.used("javbus") == limit_for(PURPOSE_PRELOAD)


def test_subscription_search_waits_for_list_throttle_before_fetch():
    """用户口径第 2 条：每隔 5 分钟发起一次，间隔加随机变量。"""
    throttle = MagicMock()
    service, _, scraper = _build_subscription(list_throttle=throttle)
    scraper.get_movies.return_value = []

    service.process_subscription({"name": "订阅A", "url": "https://www.javbus.com/star/uvy"})

    throttle.wait.assert_called_once()
    scraper.get_movies.assert_called_once()


def test_list_and_detail_throttles_use_separate_counters():
    """两个节流器必须落不同的 config key，否则详情抓完会让检索白等 5 分钟。"""
    db = _FakeDb()
    list_throttle = DetailThrottle(db, MagicMock(), config_key=SUBSCRIPTION_LIST_THROTTLE_KEY)
    detail_throttle = DetailThrottle(db, MagicMock())

    list_throttle.wait()
    keys = set(db.config.keys())
    assert SUBSCRIPTION_LIST_THROTTLE_KEY in keys
    assert keys.isdisjoint({"scrape_detail_throttle"})

    # 检索刚跑过，详情不用跟着等
    assert detail_throttle.wait() == 0


def test_preload_does_not_charge_when_cache_fresh(monkeypatch):
    monkeypatch.setattr("discovery_service.time.sleep", lambda _seconds: None)
    budget = ScrapeBudget(_FakeDb(), MagicMock())
    service, db, _ = _build_discovery(budget=budget)
    db.get_cache_full.return_value = {"value": [{"id": "fresh"}], "updated_at": time.time()}

    service.preload_discovery_cache(max_pages=1, force_refresh=False)

    # 缓存还新就不该回源，更不该消耗额度。
    assert budget.used("javbus") == 0
    service.discovery_scraper.get_javbus_hot.assert_not_called()


def test_detail_throttle_waits_between_fetches():
    stop_event = _FakeStopEvent()
    throttle = DetailThrottle(_FakeDb(), MagicMock(), stop_event=stop_event)

    assert throttle.wait() == 0  # 首次不等待
    throttle.wait()

    assert len(stop_event.waits) == 1
    # 默认节奏 = 0 ~ 3 分钟随机（用户 2026-11-05 口径）
    assert 0 <= stop_event.waits[0] <= 180


def test_detail_throttle_follows_schedule_key():
    """传了 schedule_key 就只认设置页里的值，不再用构造参数的兜底。"""
    stop_event = _FakeStopEvent()
    db = _FakeDb()
    db.config["scrape_schedule_v1"] = {"subscription_list_gap": {"value": 10, "unit": "minute"}}
    throttle = DetailThrottle(
        db,
        MagicMock(),
        stop_event=stop_event,
        schedule_key="subscription_list_gap",
    )

    throttle.wait()
    throttle.wait()

    assert len(stop_event.waits) == 1
    assert 0 <= stop_event.waits[0] <= 600


def test_detail_throttle_jitter_is_not_fixed():
    stop_event = _FakeStopEvent()
    throttle = DetailThrottle(_FakeDb(), MagicMock(), stop_event=stop_event)

    throttle.wait()
    for _ in range(8):
        throttle.wait()

    # 固定 300 秒整是可识别的节奏，抖动必须真的生效。
    assert len(set(stop_event.waits)) > 1


def test_subscription_gaps_details_within_batch(monkeypatch):
    """同一条订阅内的多个新片之间只隔几秒（默认 0~6s），不是 3 分钟。

    按 3 分钟算，一条订阅带十几个新片就吃掉半小时，308 条一轮 20+ 小时，
    「12 小时一轮」永远到不了。跨订阅的节奏由 list_throttle 管。
    """
    sleeps = []
    monkeypatch.setattr("subscription_service.time.sleep", lambda seconds: sleeps.append(seconds))
    service, _, scraper = _build_subscription()
    scraper.get_movies.return_value = [
        {"id": "ABC-123", "title": "片名", "detail_url": "https://www.javbus.com/ABC-123"}
    ]
    scraper.get_movie_detail.return_value = {"magnets": []}

    service.process_subscription({"name": "订阅A", "url": "https://www.javbus.com"})

    scraper.get_movie_detail.assert_called_once()
    assert sleeps, "详情之间必须有一次间隔"
    assert 0 <= sleeps[0] <= 6, sleeps[0]


def test_subscription_skips_detail_fetch_for_downloaded_movie(monkeypatch):
    monkeypatch.setattr("subscription_service.time.sleep", lambda seconds: None)
    service, db, scraper = _build_subscription()
    db.is_movie_downloaded.return_value = True
    scraper.get_movies.return_value = [
        {"id": "ABC-123", "title": "片名", "detail_url": "https://www.javbus.com/ABC-123"}
    ]

    report = service.process_subscription({"name": "订阅A", "url": "https://www.javbus.com"})

    scraper.get_movie_detail.assert_not_called()
    assert report["logs"][0]["reason"] == "already_downloaded"
