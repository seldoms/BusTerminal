"""2026-11-11 业务体检的三项修复回归测试。

1. 生命周期事件重复评估去重（KI-080）
2. download_queue 与 qB 对账（KI-081）
3. 存量磁力标注在选种入口实时重算（KI-082）
"""

import time
import types
from typing import Any, Dict, List

import pytest

import database
import download_service
import magnet_availability
import scraper


# ---------------------------------------------------------------------------
# 1. 事件去重
# ---------------------------------------------------------------------------

def test_record_lifecycle_event_dedupes_consecutive_repeat_within_day(main_module):
    """同片 + 同事件 + 同原因，同一 UTC 日只记一条。"""
    db = main_module.db
    movie_id = "BIZ-DEDUP-001"
    first = db.record_lifecycle_event(
        movie_id, "download_permission_denied",
        reason="daily_limit_reached", dedupe_repeat_within_day=True,
    )
    second = db.record_lifecycle_event(
        movie_id, "download_permission_denied",
        reason="daily_limit_reached", dedupe_repeat_within_day=True,
    )
    assert first is True
    assert second is False
    events = db.get_movie_lifecycle_events(movie_id, 20)
    assert len([e for e in events if e["event_type"] == "download_permission_denied"]) == 1


def test_record_lifecycle_event_rededupes_after_interleaved_event(main_module):
    """中间插入别的事件后再被拒，属于新的状态变化，必须重新记录。"""
    db = main_module.db
    movie_id = "BIZ-DEDUP-002"
    db.record_lifecycle_event(
        movie_id, "download_permission_denied",
        reason="daily_limit_reached", dedupe_repeat_within_day=True,
    )
    db.record_lifecycle_event(movie_id, "download_queued", reason="allowed")
    again = db.record_lifecycle_event(
        movie_id, "download_permission_denied",
        reason="daily_limit_reached", dedupe_repeat_within_day=True,
    )
    assert again is True
    events = db.get_movie_lifecycle_events(movie_id, 20)
    denied = [e for e in events if e["event_type"] == "download_permission_denied"]
    assert len(denied) == 2


def test_record_lifecycle_event_dedupe_is_reason_scoped(main_module):
    """拒绝原因变了就是新信息，不能被去重吃掉。"""
    db = main_module.db
    movie_id = "BIZ-DEDUP-003"
    db.record_lifecycle_event(
        movie_id, "download_permission_denied",
        reason="daily_limit_reached", dedupe_repeat_within_day=True,
    )
    again = db.record_lifecycle_event(
        movie_id, "download_permission_denied",
        reason="disk_below_threshold", dedupe_repeat_within_day=True,
    )
    assert again is True
    events = db.get_movie_lifecycle_events(movie_id, 20)
    assert {e["reason"] for e in events} == {"daily_limit_reached", "disk_below_threshold"}


def test_record_lifecycle_event_dedupe_is_opt_in(main_module):
    """默认行为不变：不传参数时每次都写（避免影响其他事件类型）。"""
    db = main_module.db
    movie_id = "BIZ-DEDUP-004"
    db.record_lifecycle_event(movie_id, "content_discovered", reason="same")
    db.record_lifecycle_event(movie_id, "content_discovered", reason="same")
    events = db.get_movie_lifecycle_events(movie_id, 20)
    assert len(events) == 2


def test_download_permission_denied_enables_daily_dedupe():
    """调用链必须真的开启去重（旧代码上这条会因断言失败而挂）。"""
    captured = []
    service = download_service.DownloadService.__new__(download_service.DownloadService)
    service._check_block_keyword = lambda *args, **kwargs: {"blocked": False}
    service._check_library_presence = lambda movie_id: {"present": False}
    service.normalize_magnet_url = lambda magnet: magnet
    service.get_automation_mode = lambda: "auto_approve_low_risk"
    service.get_bool_config = lambda key, default=False: True
    service.get_auto_download_daily_limit = lambda: 0
    service.get_today_start_ts = lambda: 0.0
    service.get_qb_session = lambda: (None, None)
    service.db = types.SimpleNamespace(
        is_movie_downloaded=lambda movie_id: False,
        get_local_file=lambda movie_id: None,
        is_movie_pending=lambda movie_id: False,
        get_active_queue_entry=lambda **kwargs: None,
        count_download_queue_since=lambda since, automated_only=False: 0,
        record_lifecycle_event=lambda movie_id, event_type, **kwargs: (
            captured.append((movie_id, event_type, kwargs)) or True
        ),
    )

    result = service.evaluate_download_permission(
        "BIZ-DEDUP-CALL", _magnet("9" * 40), "some title", "http://source"
    )
    assert result["allowed"] is False
    assert "daily_limit_reached" in result["reasons"]
    assert len(captured) == 1
    assert captured[0][1] == "download_permission_denied"
    assert captured[0][2].get("dedupe_repeat_within_day") is True


def test_apply_queue_reconciliation_updates_only_active_rows(main_module):
    """对账批量写只作用于 active 状态，不覆盖并发下已变化的行。"""
    db = main_module.db
    conn = db.get_connection()
    try:
        cursor = conn.cursor()
        p = "%s" if db.is_postgres else "?"
        now = time.time()
        for task_id, status in (
            ("BIZ-RECON-ROW-1", "submitted"),
            ("BIZ-RECON-ROW-2", "cancelled"),
        ):
            cursor.execute(
                f"INSERT INTO download_queue (id, magnet, movie_id, status, created_at, updated_at) "
                f"VALUES ({p},{p},{p},{p},{p},{p})",
                (task_id, "magnet:" + task_id, task_id, status, now, now),
            )
        conn.commit()
    finally:
        db._release_connection(conn)

    applied = db.apply_queue_reconciliation([
        {"id": "BIZ-RECON-ROW-1", "status": "cancelled", "error_message": "reconciled: torrent_absent_from_qb"},
        {"id": "BIZ-RECON-ROW-2", "status": "cancelled", "error_message": "must not touch"},
    ])
    assert applied == 1

    conn = db.get_connection()
    try:
        cursor = conn.cursor()
        p = "%s" if db.is_postgres else "?"
        cursor.execute(
            f"SELECT id, status, error_message FROM download_queue WHERE id IN ({p},{p})",
            ("BIZ-RECON-ROW-1", "BIZ-RECON-ROW-2"),
        )
        rows = {dict(r)["id"]: dict(r) for r in cursor.fetchall()}
    finally:
        db._release_connection(conn)
    assert rows["BIZ-RECON-ROW-1"]["error_message"] == "reconciled: torrent_absent_from_qb"
    assert rows["BIZ-RECON-ROW-2"]["error_message"] is None


# ---------------------------------------------------------------------------
# 2. 队列对账
# ---------------------------------------------------------------------------

class _FakeDb:
    def __init__(self, rows: List[Dict[str, Any]]):
        self.rows = rows
        self.applied: List[Dict[str, Any]] = []

    def get_active_queue_entries_for_reconcile(self):
        return [dict(r) for r in self.rows]

    def apply_queue_reconciliation(self, updates):
        self.applied = list(updates)
        return len(updates)


def _make_service(rows, torrents):
    service = download_service.DownloadService.__new__(download_service.DownloadService)
    service.db = _FakeDb(rows)
    service.qb_api = types.SimpleNamespace(
        get_torrents=lambda session, base_url, **kwargs: list(torrents)
    )
    service.get_qb_session = lambda: (object(), "http://qb")
    service.safe_exception_label = lambda exc: type(exc).__name__
    return service


def _magnet(info_hash: str) -> str:
    return f"magnet:?xt=urn:btih:{info_hash}&dn=test"


def test_reconcile_cancels_absent_and_dead_torrents():
    now = time.time()
    old = now - 40 * 86400
    fresh = now - 3600
    alive_hash = "a" * 40
    dead_hash = "b" * 40
    fresh_hash = "c" * 40
    rows = [
        {"id": "T-ABSENT", "magnet": _magnet("d" * 40), "created_at": old, "status": "submitted"},
        {"id": "T-DEAD", "magnet": _magnet(dead_hash), "created_at": old, "status": "submitted"},
        {"id": "T-ALIVE", "magnet": _magnet(alive_hash), "created_at": old, "status": "submitted"},
        {"id": "T-FRESH", "magnet": _magnet(fresh_hash), "created_at": fresh, "status": "submitted"},
        {"id": "T-BAD", "magnet": "not-a-magnet", "created_at": old, "status": "submitted"},
    ]
    torrents = [
        {"hash": alive_hash, "state": "downloading", "added_on": old},
        {"hash": dead_hash, "state": "metaDL", "added_on": old},
    ]
    service = _make_service(rows, torrents)
    report = service.reconcile_download_queue_once()
    assert report["success"] is True
    assert report["cancelled_absent"] == 1
    assert report["cancelled_dead"] == 1
    assert report["unparseable"] == 1
    assert report["unchanged"] == 2
    assert report["applied"] == 2
    by_id = {u["id"]: u for u in service.db.applied}
    assert by_id["T-ABSENT"]["status"] == "cancelled"
    assert "torrent_absent_from_qb" in by_id["T-ABSENT"]["error_message"]
    assert "dead_torrent_metaDL" in by_id["T-DEAD"]["error_message"]


def test_reconcile_dry_run_does_not_write():
    old = time.time() - 40 * 86400
    rows = [{"id": "T-1", "magnet": _magnet("e" * 40), "created_at": old, "status": "submitted"}]
    service = _make_service(rows, [])
    report = service.reconcile_download_queue_once(dry_run=True)
    assert report["dry_run"] is True
    assert report["pending_updates"] == 1
    assert report["applied"] == 0
    assert service.db.applied == []


def test_reconcile_matches_base32_btih():
    """队列里是 32 位 base32、qB 返回 40 位 hex 时也要能对上。"""
    import base64
    info_hash_hex = "f" * 40
    base32 = base64.b32encode(bytes.fromhex(info_hash_hex)).decode().upper()
    old = time.time() - 40 * 86400
    rows = [{"id": "T-B32", "magnet": _magnet(base32), "created_at": old, "status": "submitted"}]
    torrents = [{"hash": info_hash_hex, "state": "downloading", "added_on": old}]
    report = _make_service(rows, torrents).reconcile_download_queue_once()
    assert report["unchanged"] == 1
    assert report["cancelled_absent"] == 0


def test_reconcile_handles_qb_unavailable():
    service = download_service.DownloadService.__new__(download_service.DownloadService)
    service.get_qb_session = lambda: (None, None)
    report = service.reconcile_download_queue_once()
    assert report["success"] is False


def test_active_queue_entries_exclude_pending(main_module):
    """pending 还没提交到 qB，不能进入对账候选。"""
    db = main_module.db
    conn = db.get_connection()
    try:
        cursor = conn.cursor()
        p = "%s" if db.is_postgres else "?"
        now = time.time()
        for task_id, status in (("BIZ-PENDING-1", "pending"), ("BIZ-SUBMITTED-1", "submitted")):
            cursor.execute(
                f"INSERT INTO download_queue (id, magnet, movie_id, status, created_at, updated_at) "
                f"VALUES ({p},{p},{p},{p},{p},{p})",
                (task_id, "magnet:" + task_id, task_id, status, now, now),
            )
        conn.commit()
    finally:
        db._release_connection(conn)
    ids = {r["id"] for r in db.get_active_queue_entries_for_reconcile()}
    assert "BIZ-PENDING-1" not in ids
    assert "BIZ-SUBMITTED-1" in ids


# ---------------------------------------------------------------------------
# 3. 磁力标注实时重算
# ---------------------------------------------------------------------------

def _bare_scraper():
    # 解析/选片不依赖实例状态，绕过重量级 __init__（会连站、建会话）。
    return scraper.JavBusScraper.__new__(scraper.JavBusScraper)


def test_info_hash_to_hex_accepts_magnet_and_bare_hash():
    import base64
    info_hash_hex = "1" * 40
    base32 = base64.b32encode(bytes.fromhex(info_hash_hex)).decode().upper()
    assert magnet_availability.info_hash_to_hex(_magnet(info_hash_hex)) == info_hash_hex
    assert magnet_availability.info_hash_to_hex(_magnet(base32)) == info_hash_hex
    assert magnet_availability.info_hash_to_hex(info_hash_hex) == info_hash_hex
    assert magnet_availability.info_hash_to_hex("nope") is None


def test_refresh_magnet_metadata_fixes_stale_resolution():
    s = _bare_scraper()
    magnets = [
        {
            "title": "AVOP-069.HD高清", "size": "4.96GB",
            "magnet": _magnet("2" * 40),
            "resolution": None, "has_subtitle": False, "size_bytes": 0,
        },
        {
            "title": "AVOP-069-FullHD-中文字幕", "size": "2.10GB",
            "magnet": _magnet("3" * 40),
            "resolution": "720P", "has_subtitle": True, "size_bytes": 0,
        },
        {
            "title": "AVOP-069.mp4", "size": "",
            "magnet": _magnet("4" * 40),
            "resolution": None, "has_subtitle": False, "size_bytes": 12345,
        },
    ]
    result = s._refresh_magnet_metadata(magnets)
    assert result is magnets  # 原地改，保证 select_alive_magnet 的对象引用不变
    assert magnets[0]["resolution"] == "720P"       # 中文「高清」保守判 720P
    assert magnets[0]["has_subtitle"] is False      # 此条不含字幕标记
    assert magnets[0]["size_bytes"] > 0
    assert magnets[1]["resolution"] == "1080P"      # FullHD 旧实现误判成 720P
    assert magnets[2]["size_bytes"] == 12345        # size 为空时不能抹掉已有体积


def test_select_best_magnet_uses_refreshed_metadata():
    """旧缓存标注会把候选分到错的组，重算后偏好组应当命中真正匹配的那个。

    旧代码（v2.20）：FullHD 被裸子串误标成 720P、「超清」是 NULL，
    preference=1080P 找不到组 → 降级池按体积选了 4.96GB 的「超清」。
    重算后 FullHD=1080P、超清=4K → 1080P 组命中 FullHD。
    """
    s = _bare_scraper()
    magnets = [
        {"title": "ABC-123-FullHD", "size": "1.00GB", "magnet": _magnet("5" * 40),
         "resolution": "720P", "has_subtitle": False, "size_bytes": 1073741824},
        {"title": "ABC-123-超清", "size": "4.96GB", "magnet": _magnet("6" * 40),
         "resolution": None, "has_subtitle": False, "size_bytes": 5325759447},
    ]
    best = s.select_best_magnet(magnets, preference="1080P")
    assert best["title"] == "ABC-123-FullHD"
    assert best["resolution"] == "1080P"
    assert magnets[1]["resolution"] == "4K"
