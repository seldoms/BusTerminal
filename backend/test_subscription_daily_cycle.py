"""订阅周期改成每天一次（2026-11-09 用户口径：订阅每天刷新一次就行）。

背景：原来默认 12 小时一轮，但实测一轮实际要 9~14 小时（377 条 × 每条 0~3 分钟
随机间隔）—— 设 12 小时根本没意义，轮次根本跑不完。改成 1 天是如实反映现实，
顺带把对源站的请求频率降下来。
"""

import scrape_schedule


def test_subscription_cycle_defaults_to_one_day():
    value, unit = scrape_schedule.resolve("subscription_cycle")
    assert unit == "day"
    assert value == 1
    assert scrape_schedule.interval_seconds("subscription_cycle") == 86400


def test_subscription_cycle_is_not_shorter_than_a_day_by_default():
    """默认周期不得短于 1 天（否则一轮还没跑完就又开一轮）。"""
    assert scrape_schedule.interval_seconds("subscription_cycle") >= 86400


def test_user_can_still_override_to_hours():
    """设置页仍可自定义（不锁死，只是默认值变了）。"""
    class _Db:
        def get_config(self, key):
            # get_overrides 期望 get_config 直接返回 dict（DB 层负责解析 JSON）
            return {"subscription_cycle": {"value": 6, "unit": "hour"}}

    value, unit = scrape_schedule.resolve("subscription_cycle", _Db())
    assert value == 6
    assert unit == "hour"


def test_snapshot_reports_day_unit():
    snapshot = scrape_schedule.snapshot()
    items = {item["key"]: item for item in snapshot["items"]}
    entry = items["subscription_cycle"]
    assert entry["value"] == 1
    assert entry["unit"] == "day"
    assert entry["seconds"] == 86400