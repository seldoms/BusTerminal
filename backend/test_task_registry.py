"""后台任务注册表的两处可观测性修正（2026-11-04）。

1. `mark_heartbeat`：给「每小时唤醒、命中条件才干活」的循环用。默认关闭的功能
   （比如 `auto_delete_watched_daily`）一整天都不会 mark_success，心跳永远停在
   容器启动时刻，看起来像卡死。
2. `_safe_error_message`：旧正则只认单 token，`psycopg2.pool.PoolError` 的消息
   `connection pool exhausted` 带空格 → 整条退回成类名，任务卡片上只剩
   `type=PoolError, message=PoolError`，现场完全看不出是「池耗尽」还是「池已关闭」。
"""

from task_registry import TaskRegistry


def test_mark_heartbeat_updates_time_without_clearing_errors():
    registry = TaskRegistry()
    registry.register("demo", description="d", category="maintenance")
    registry.mark_started("demo")

    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        registry.mark_error("demo", exc)

    before = registry.snapshot()["tasks"][0]
    assert before["consecutive_error_count"] == 1
    heartbeat_before = before["last_heartbeat_at"]

    registry.mark_heartbeat("demo", summary={"enabled": False})
    after = registry.snapshot()["tasks"][0]

    assert after["last_heartbeat_at"] >= heartbeat_before
    # 心跳不能把错误抹掉 —— 那会让真故障静默消失
    assert after["consecutive_error_count"] == 1
    assert after["last_error"]["type"] == "RuntimeError"
    assert after["last_summary"] == {"enabled": False}


def test_heartbeat_does_not_mark_success():
    registry = TaskRegistry()
    registry.register("demo", description="d", category="maintenance")
    registry.mark_started("demo")
    registry.mark_heartbeat("demo")
    task = registry.snapshot()["tasks"][0]
    assert task["last_success_at"] is None


def test_mark_heartbeat_registers_unknown_task():
    registry = TaskRegistry()
    registry.mark_heartbeat("late_task")
    names = [task["name"] for task in registry.snapshot()["tasks"]]
    assert names == ["late_task"]


def test_pool_exhausted_message_survives():
    """真正想看的现场信息：是耗尽还是已关闭。"""
    class PoolError(Exception):
        pass

    assert TaskRegistry._safe_error_message(PoolError("connection pool exhausted")) == \
        "connection pool exhausted"
    assert TaskRegistry._safe_error_message(PoolError("connection pool closed")) == \
        "connection pool closed"
    assert TaskRegistry._safe_error_message(
        Exception("server closed the connection unexpectedly")
    ) == "server closed the connection unexpectedly"


def test_urls_and_paths_still_redacted():
    """放宽不能把 URL / 文件路径 / 中文路径带出去。"""
    class Boom(Exception):
        pass

    cases = [
        "http://nas.local:8096?api_key=very-secret-token",
        "/mnt/storage/PT/movie/SSIS-836.mp4",
        "Media/SSIS-836.mp4",
        "连不上字幕站：https://subtitlecat.com/index.php",
        "failed\n\tat /app/backend/main.py",
    ]
    for text in cases:
        assert TaskRegistry._safe_error_message(Boom(text)) == "Boom", text


def test_empty_message_falls_back_to_class_name():
    class Boom(Exception):
        pass

    assert TaskRegistry._safe_error_message(Boom("")) == "Boom"
    assert TaskRegistry._safe_error_message(Boom(None)) == "Boom"
