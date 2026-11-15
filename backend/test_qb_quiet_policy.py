"""周末全天静默策略：下载闸门 / groom / dispatcher 必须一起停。

只停「传输」是不够的 —— groom 把「已完成 + 已暂停」当成做种时限达成，
静默一暂停种子反而会触发一串 relocate 搬大文件。这里用会炸的假 qB 断言
静默期整条 qB 链路一次都不碰。
"""
import logging
from types import SimpleNamespace

from download_service import DownloadService


class BoomQB:
    """任何属性访问都算测试失败：静默期不该碰 qB。"""

    def __getattr__(self, name):
        raise AssertionError("qB touched during quiet hours: " + name)


class FakeDB:
    def __init__(self):
        self.events = []

    def get_config(self, key):
        return None

    def get_active_queue_entry(self, **kwargs):
        return None

    def record_lifecycle_event(self, *args, **kwargs):
        self.events.append((args, kwargs))


class FakeRegistry:
    def __init__(self):
        self.successes = []

    def mark_success(self, name, **kwargs):
        self.successes.append((name, kwargs.get("summary")))

    def mark_error(self, name, exc):
        raise AssertionError("unexpected error: " + name)


class FakeStop:
    """wait 一次就退出，保证后台循环只跑一轮。"""

    def __init__(self):
        self.done = False

    def is_set(self):
        return self.done

    def wait(self, seconds):
        self.done = True
        return True


def make_service(quiet=True, boom_qb=True):
    qb = BoomQB() if boom_qb else _WorkingQB()
    return DownloadService(
        db=FakeDB(),
        qb_api=qb,
        config=SimpleNamespace(
            GROOM_TASK_INTERVAL=1,
            DOWNLOAD_DISPATCHER_INTERVAL=1,
            JUNK_DELETE_TORRENTS=False,
        ),
        task_registry=FakeRegistry(),
        logger=logging.getLogger("test_quiet_policy"),
        stop_event=FakeStop(),
        normalize_magnet_url=lambda url: url,
        get_bool_config=lambda key, fallback: fallback,
        get_automation_mode=lambda: "manual",
        get_auto_download_daily_limit=lambda: 0,
        get_today_start_ts=lambda: 0,
        get_qb_session=lambda: (object(), "http://qb.local"),
        get_disk_threshold_bytes=lambda: 0,
        safe_exception_label=lambda exc: type(exc).__name__,
        display_value_for_log=str,
        display_path_for_log=str,
        safe_number=lambda value, default=0: float(value or default),
        parse_int_value=lambda value, default=0, minimum=None: int(value or default),
        extract_movie_id_from_text=lambda text: None,
        get_qb_delivery_folder=lambda: "/hp/_incoming",
        is_quiet_now=lambda: quiet,
    )


class _WorkingQB:
    def get_server_state(self, session, base_url):
        return {"free_space_on_disk": 10 ** 13}


def test_permission_gate_blocks_new_downloads_without_touching_qb():
    service = make_service(quiet=True)
    result = service.evaluate_download_permission(
        None, "magnet:?xt=urn:btih:" + "a" * 40, "TEST-001", None, automated=False,
    )
    assert result["allowed"] is False
    assert "qb_quiet_hours" in result["reasons"]
    assert service.db.events == []          # 静默是策略预期，不灌生命周期事件


def test_permission_gate_allows_when_not_quiet():
    service = make_service(quiet=False, boom_qb=False)
    result = service.evaluate_download_permission(
        None, "magnet:?xt=urn:btih:" + "b" * 40, "TEST-002", None, automated=False,
    )
    assert result["allowed"] is True


def test_permission_gate_fails_open_when_probe_broken():
    service = make_service(quiet=False, boom_qb=False)
    service.is_quiet_now = lambda: (_ for _ in ()).throw(RuntimeError("config down"))
    result = service.evaluate_download_permission(
        None, "magnet:?xt=urn:btih:" + "c" * 40, "TEST-003", None, automated=False,
    )
    assert result["allowed"] is True


def test_groom_skips_entire_round_during_quiet_hours():
    service = make_service(quiet=True)
    service.groom_torrents_task()             # BoomQB 一旦被访问就会 AssertionError
    assert service.task_registry.successes == [
        ("groom_torrents_task", {"quiet_hours": True, "torrent_count": 0}),
    ]


def test_dispatcher_skips_entire_round_during_quiet_hours():
    service = make_service(quiet=True)
    service.download_dispatcher_task()
    assert service.task_registry.successes == [
        ("download_dispatcher_task", {
            "quiet_hours": True, "pending_count": 0, "qb_connected": False,
        }),
    ]


def test_no_probe_injected_keeps_legacy_behavior():
    """没注入 is_quiet_now 的服务（老调用方/老测试）行为完全不变。"""
    service = make_service(quiet=True)
    service.is_quiet_now = None
    assert service._quiet_active() is False
