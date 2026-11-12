"""热门投递循环（`jable_hot_auto_task_loop`）的任务卡要反映真实执行。

背景（2026-11-07 实测）：这张卡一直是 `running` + 空摘要、`last_success_at`
永远为 null —— 因为循环里**一次都没碰过 task_registry**，只在 logger 里打日志。
这个循环一天只跑两轮（08:00 / 18:00），其余时间在等下一个时刻，所以
「没报心跳」和「卡死」在任务卡上长得一模一样，看不出到底跑没跑。
"""

from unittest.mock import MagicMock

import pytest

from discovery_service import DiscoveryService


def _build_service(task_registry=None):
    db = MagicMock()
    db.get_config.return_value = None
    db.get_pending_jable_hot_items.return_value = []
    service = DiscoveryService(
        db=db,
        scraper=MagicMock(),
        jable_scraper=MagicMock(),
        discovery_scraper=MagicMock(),
        config=MagicMock(),
        logger=MagicMock(),
        stop_event=MagicMock(),
        cache_ttl_seconds=1800,
        safe_number=float,
        safe_exception_label=lambda exc: type(exc).__name__,
        display_value_for_log=str,
        normalize_magnet_url=lambda value: value,
        evaluate_download_permission=lambda *args, **kwargs: {"allowed": False},
        queue_download_if_absent=lambda *args, **kwargs: {},
        schedule_image_cache_warmup=lambda *args: None,
        task_registry=task_registry,
    )
    # 循环体只跑一轮：第一次 is_set() 为 False 进循环，第二次 True 退出
    service.stop_event.is_set.side_effect = [False, True]
    return service, db


def test_waiting_state_reports_heartbeat():
    """没到投递时刻也要报心跳 —— 否则 last_heartbeat_at 永远停在启动时刻。"""
    registry = MagicMock()
    service, _ = _build_service(task_registry=registry)
    service._due_hot_slot = MagicMock(return_value=None)

    service.jable_hot_auto_task_loop()

    registry.mark_heartbeat.assert_called_once()
    summary = registry.mark_heartbeat.call_args[1]["summary"]
    assert summary["state"] == "waiting"
    assert "slots" in summary
    registry.mark_error.assert_not_called()


def test_due_slot_marks_success_with_cycle_summary():
    """到点跑完一轮要标成功，并把这一轮的结果写进摘要。"""
    registry = MagicMock()
    service, db = _build_service(task_registry=registry)
    service._due_hot_slot = MagicMock(return_value="2026-11-07 08:00")
    service._run_jable_hot_discovery_cycle = MagicMock(
        return_value={"fetched": 60, "lists": 3, "pending": 0}
    )

    service.jable_hot_auto_task_loop()

    registry.mark_success.assert_called_once()
    summary = registry.mark_success.call_args[1]["summary"]
    assert summary["state"] == "done"
    assert summary["slot"] == "2026-11-07 08:00"
    assert summary["fetched"] == 60
    # 跑完要记住这个时刻，避免 30 分钟窗口内重复投递
    db.set_config.assert_called_once()


def test_disabled_state_is_reported_without_error():
    """功能被关掉时不是故障，只是没到活儿。"""
    registry = MagicMock()
    service, db = _build_service(task_registry=registry)
    db.get_config.return_value = False
    service._due_hot_slot = MagicMock(return_value="2026-11-07 08:00")
    service._run_jable_hot_discovery_cycle = MagicMock()

    service.jable_hot_auto_task_loop()

    service._run_jable_hot_discovery_cycle.assert_not_called()
    registry.mark_error.assert_not_called()
    summary = registry.mark_heartbeat.call_args[1]["summary"]
    assert summary["state"] == "disabled"


def test_cycle_exception_is_reported_as_error():
    """循环里真出异常要落到任务卡上，不能只进日志。"""
    registry = MagicMock()
    service, _ = _build_service(task_registry=registry)
    service._due_hot_slot = MagicMock(return_value="2026-11-07 08:00")
    service._run_jable_hot_discovery_cycle = MagicMock(side_effect=RuntimeError("boom"))

    service.jable_hot_auto_task_loop()

    registry.mark_error.assert_called_once()
    err = registry.mark_error.call_args[0][1]
    assert isinstance(err, RuntimeError)
    assert registry.mark_error.call_args[1]["summary"]["state"] == "error"


def test_missing_task_registry_does_not_break_loop():
    """没注入 task_registry 也不能让循环崩掉（老调用方/单测都这样）。"""
    service, _ = _build_service(task_registry=None)
    service._due_hot_slot = MagicMock(return_value=None)

    service.jable_hot_auto_task_loop()  # 不抛异常即通过


@pytest.mark.parametrize("state", ["waiting", "disabled", "done"])
def test_card_write_failure_never_breaks_loop(state):
    """任务卡写失败（注册表异常）不能拖垮主循环。"""
    registry = MagicMock()
    registry.mark_heartbeat.side_effect = RuntimeError("registry down")
    registry.mark_success.side_effect = RuntimeError("registry down")
    service, _ = _build_service(task_registry=registry)
    service._due_hot_slot = MagicMock(
        return_value=None if state == "waiting" else "2026-11-07 08:00"
    )
    service._run_jable_hot_discovery_cycle = MagicMock(return_value={})
    if state == "disabled":
        service.db.get_config.return_value = False

    service.jable_hot_auto_task_loop()  # 不抛异常即通过
