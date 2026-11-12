"""抓取节奏配置（设置页「抓取节奏」）的行为锁定。

用户 2026-11-05 口径：首页/榜单 4 小时、订阅整轮 12 小时、订阅检索与详情
3 分钟内随机、元数据 7 天、字幕 3 分钟内随机、头像 7 天、热门投递 08:00/18:00。
"""

import pytest

import scrape_schedule


class _FakeDb:
    def __init__(self, config=None):
        self.config = dict(config or {})

    def get_config(self, key):
        return self.config.get(key)

    def set_config(self, key, value):
        self.config[key] = value


def test_defaults_match_user_spec():
    db = _FakeDb()
    assert scrape_schedule.interval_seconds("feed_preload", db) == 4 * 3600
    # 订阅整轮：2026-11-09 用户改为「每天刷新一次」（原 12 小时 —— 但实测一轮
    # 要 9~14 小时，12 小时的周期根本没意义，轮次根本跑不完）
    assert scrape_schedule.interval_seconds("subscription_cycle", db) == 24 * 3600
    assert scrape_schedule.interval_seconds("subscription_list_gap", db) == 3 * 60
    # 详情是「同一条订阅内的多个新片之间」，默认 6 秒；按 3 分钟算一轮永远跑不完
    assert scrape_schedule.interval_seconds("subscription_detail_gap", db) == 6
    assert scrape_schedule.interval_seconds("actress_metadata", db) == 7 * 86400
    assert scrape_schedule.interval_seconds("subtitle_backfill", db) == 3 * 60
    assert scrape_schedule.interval_seconds("actress_avatar", db) == 7 * 86400
    assert scrape_schedule.time_slots(db=db) == ["08:00", "18:00"]


def test_config_override_wins_over_default(monkeypatch):
    monkeypatch.delenv("FEED_PRELOAD_REFRESH_SECONDS", raising=False)
    db = _FakeDb({"scrape_schedule_v1": {"feed_preload": {"value": 90, "unit": "minute"}}})
    assert scrape_schedule.interval_seconds("feed_preload", db) == 90 * 60


def test_env_vars_are_ignored(monkeypatch):
    """频率只允许一个入口。

    2026-11-05 上线实测：生产 compose 还留着 FEED_PRELOAD_REFRESH_SECONDS=10800，
    设置页显示「未自定义」却跑着 3 小时 —— 用户看到的和实际跑的不是一回事。
    """
    monkeypatch.setenv("FEED_PRELOAD_REFRESH_SECONDS", "7200")
    monkeypatch.setenv("GFRIENDS_AVATAR_SYNC_INTERVAL_SECONDS", "1800")
    db = _FakeDb()
    assert scrape_schedule.interval_seconds("feed_preload", db) == 4 * 3600
    assert scrape_schedule.interval_seconds("actress_avatar", db) == 7 * 86400


def test_random_delay_stays_within_range():
    db = _FakeDb({"scrape_schedule_v1": {"subtitle_backfill": {"value": 3, "unit": "minute"}}})
    for _ in range(50):
        delay = scrape_schedule.random_delay_seconds("subtitle_backfill", db)
        assert 0 <= delay <= 180


def test_zero_interval_falls_back_to_minimum_gap():
    """0 秒等于对站点连续开火，脏配置必须被兜到最小间隔。"""
    db = _FakeDb({"scrape_schedule_v1": {"subtitle_backfill": {"value": 0, "unit": "minute"}}})
    assert scrape_schedule.random_delay_seconds("subtitle_backfill", db) == scrape_schedule.MIN_RANDOM_GAP_SECONDS


def test_validate_rejects_unknown_key_and_bad_units():
    errors = scrape_schedule.validate_payload(
        {
            "nope": {"value": 1, "unit": "hour"},
            "feed_preload": {"value": 4, "unit": "week"},
        }
    )
    assert "schedule_unknown_key:nope" in errors
    assert "feed_preload_invalid_unit" in errors


def test_validate_rejects_out_of_range():
    errors = scrape_schedule.validate_payload({"feed_preload": {"value": 0, "unit": "minute"}})
    assert "feed_preload_below_min" in errors
    errors = scrape_schedule.validate_payload({"subscription_cycle": {"value": 999, "unit": "day"}})
    assert "subscription_cycle_above_max" in errors


def test_validate_time_slots():
    assert scrape_schedule.validate_payload({"jable_hot": {"times": ["08:00", "18:00"]}}) == []
    assert "jable_hot_invalid_time" in scrape_schedule.validate_payload(
        {"jable_hot": {"times": ["25:00"]}}
    )
    assert "jable_hot_times_required" in scrape_schedule.validate_payload({"jable_hot": {"times": []}})
    too_many = [f"{hour:02d}:00" for hour in range(9)]
    assert "jable_hot_too_many_times" in scrape_schedule.validate_payload(
        {"jable_hot": {"times": too_many}}
    )


def test_time_slots_sorted_and_deduped():
    db = _FakeDb({"scrape_schedule_v1": {"jable_hot": {"times": ["18:00", "08:00", "18:00"]}}})
    assert scrape_schedule.time_slots("jable_hot", db) == ["08:00", "18:00"]


def test_time_slots_fall_back_when_config_is_garbage():
    db = _FakeDb({"scrape_schedule_v1": {"jable_hot": {"times": ["not-a-time"]}}})
    assert scrape_schedule.time_slots("jable_hot", db) == ["08:00", "18:00"]


def test_save_merges_without_dropping_other_keys():
    db = _FakeDb({"scrape_schedule_v1": {"feed_preload": {"value": 6, "unit": "hour"}}})
    snapshot = scrape_schedule.save_payload(
        {"subtitle_backfill": {"value": 5, "unit": "minute"}}, db
    )
    stored = db.get_config("scrape_schedule_v1")
    assert stored["feed_preload"] == {"value": 6, "unit": "hour"}
    assert stored["subtitle_backfill"] == {"value": 5, "unit": "minute"}
    assert snapshot["config_key"] == "scrape_schedule_v1"
    keys = {item["key"] for item in snapshot["items"]}
    assert {"feed_preload", "subtitle_backfill", "jable_hot"} <= keys


def test_snapshot_reports_effective_seconds():
    db = _FakeDb()
    items = {item["key"]: item for item in scrape_schedule.snapshot(db)["items"]}
    assert items["feed_preload"]["value"] == 4
    assert items["feed_preload"]["unit"] == "hour"
    assert items["feed_preload"]["seconds"] == 4 * 3600
    assert items["feed_preload"]["units"][0]["label"]
    assert items["jable_hot"]["kind"] == "times"
    assert items["jable_hot"]["times"] == ["08:00", "18:00"]


def test_read_failure_degrades_to_defaults():
    class BrokenDb:
        def get_config(self, key):
            raise RuntimeError("db down")

        def set_config(self, key, value):
            raise RuntimeError("db down")

    assert scrape_schedule.interval_seconds("feed_preload", BrokenDb()) == 4 * 3600
    assert scrape_schedule.time_slots("jable_hot", BrokenDb()) == ["08:00", "18:00"]


@pytest.mark.parametrize(
    "value,unit,expected",
    [
        (4, "hour", 4 * 3600),
        (240, "minute", 240 * 60),
        (1, "day", 86400),
    ],
)
def test_unit_conversion(value, unit, expected):
    db = _FakeDb({"scrape_schedule_v1": {"feed_preload": {"value": value, "unit": unit}}})
    assert scrape_schedule.interval_seconds("feed_preload", db) == expected


def _build_discovery(db):
    from unittest.mock import MagicMock

    from discovery_service import DiscoveryService

    return DiscoveryService(
        db=db,
        scraper=MagicMock(),
        jable_scraper=MagicMock(),
        discovery_scraper=MagicMock(),
        config=MagicMock(),
        logger=MagicMock(),
        stop_event=__import__("threading").Event(),
        cache_ttl_seconds=1800,
        safe_number=float,
        safe_exception_label=lambda exc: type(exc).__name__,
        display_value_for_log=str,
        normalize_magnet_url=lambda value: value,
        evaluate_download_permission=lambda *args, **kwargs: {"allowed": False},
        queue_download_if_absent=lambda *args, **kwargs: {},
        schedule_image_cache_warmup=lambda *args: None,
        scrape_budget=None,
    )


def test_hot_slot_fires_once_per_time_slot():
    """热门投递：08:00 窗口内只跑一次，跑过要记账，避免 30 分钟内每分钟重复投递。"""
    from datetime import datetime

    db = _FakeDb()
    service = _build_discovery(db)

    first = service._due_hot_slot(datetime(2026, 11, 5, 8, 5))
    assert first == "2026-11-05 08:00"
    db.set_config("jable_hot_last_slot", first)
    assert service._due_hot_slot(datetime(2026, 11, 5, 8, 6)) is None
    # 18:00 是另一个时刻，照常触发
    assert service._due_hot_slot(datetime(2026, 11, 5, 18, 1)) == "2026-11-05 18:00"


def test_hot_slot_respects_window():
    from datetime import datetime

    db = _FakeDb()
    service = _build_discovery(db)

    assert service._due_hot_slot(datetime(2026, 11, 5, 7, 59)) is None
    assert service._due_hot_slot(datetime(2026, 11, 5, 8, 31)) is None


def test_hot_slot_uses_custom_times():
    from datetime import datetime

    db = _FakeDb()
    db.config["scrape_schedule_v1"] = {"jable_hot": {"times": ["12:00"]}}
    service = _build_discovery(db)

    assert service._due_hot_slot(datetime(2026, 11, 5, 8, 5)) is None
    assert service._due_hot_slot(datetime(2026, 11, 5, 12, 0)) == "2026-11-05 12:00"
