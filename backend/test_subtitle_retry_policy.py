"""字幕补抓候选的分级冷却（KI-062）。

2026-11-04 实测的两个浪费：
1. `IPX867` 站点返回的是空 SRT（`invalid`），旧查询让它**每轮 backfill 都被捞出来**
   白问一次；
2. `video_missing` 被记成 `skipped`，既不算有字幕、又永远不在重试范围 —— 卡在中间
   永远不会被任何人处理。

所以这里锁三件事：
- `invalid` / `not_found` 走 7 天冷却，`error` 走 6 小时短冷却，`NULL` 立刻捞；
- `ok` / `path_missing` / `skipped` 永不捞；
- 影片文件不在时落库状态是 `path_missing`，不是 `skipped`。
"""

import time

import config
import database as database_module
from subtitle_fetcher import db_record_for_result

DAY = 86400


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{tmp_path / 'subs.db'}")
    return database_module.Database()


def _seed(db, movie_id, status, fetched_at=None):
    conn = db.get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO movies (id, title, code_norm) VALUES (?, ?, ?)",
            (movie_id, f"{movie_id} 标题", movie_id.replace("_", "-")),
        )
        cursor.execute(
            "INSERT INTO local_files (movie_id, file_path, added_at, subtitle_status, subtitle_fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (movie_id, f"/hp/A/{movie_id}/{movie_id}.mp4", time.time(), status, fetched_at),
        )
        conn.commit()
    finally:
        db._release_connection(conn)


def _candidates(db, **kwargs):
    return {row["movie_id"] for row in db.get_local_files_missing_subtitle(**kwargs)}


def test_never_tried_is_picked_immediately(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch)
    _seed(db, "AAA_001", None)
    assert "AAA_001" in _candidates(db)


def test_empty_srt_waits_a_week_instead_of_retrying_every_round(tmp_path, monkeypatch):
    """IPX867 那种「站点给了个空 SRT」不该每轮都白问一次。"""
    db = _fresh_db(tmp_path, monkeypatch)
    now = time.time()
    _seed(db, "BAD_001", "invalid", now - 2 * DAY)
    _seed(db, "BAD_002", "invalid", now - 8 * DAY)
    picked = _candidates(db)
    assert "BAD_001" not in picked, "2 天前刚确认站点给的是空 SRT，不该马上再问"
    assert "BAD_002" in picked, "过了 7 天冷却应该重新排队"


def test_not_found_waits_a_week(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch)
    now = time.time()
    _seed(db, "MISS_001", "not_found", now - DAY)
    _seed(db, "MISS_002", "not_found", now - 8 * DAY)
    picked = _candidates(db)
    assert "MISS_001" not in picked
    assert "MISS_002" in picked


def test_transient_error_retries_after_six_hours(tmp_path, monkeypatch):
    """网络抖动只该等 6 小时，不该按 not_found 等 7 天。"""
    db = _fresh_db(tmp_path, monkeypatch)
    now = time.time()
    _seed(db, "ERR_001", "error", now - 3600)
    _seed(db, "ERR_002", "error", now - 7 * 3600)
    picked = _candidates(db)
    assert "ERR_001" not in picked, "1 小时前刚失败，先别连着打"
    assert "ERR_002" in picked, "过了 6 小时应该重试"


def test_ok_and_path_missing_are_never_picked(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch)
    now = time.time()
    _seed(db, "OK_001", "ok", now - 365 * DAY)
    _seed(db, "GONE_001", "path_missing", now - 365 * DAY)
    _seed(db, "SKIP_001", "skipped", None)
    assert _candidates(db) == set()


def test_error_cooldown_is_configurable(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch)
    _seed(db, "ERR_003", "error", time.time() - 120)
    assert "ERR_003" not in _candidates(db)
    assert "ERR_003" in _candidates(db, error_retry_seconds=60)


def test_missing_video_is_recorded_as_path_missing_not_skipped():
    record = db_record_for_result({"status": "path_missing", "reason": "video_missing"})
    assert record["status"] == "path_missing"


def test_limit_still_respected(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch)
    for index in range(5):
        _seed(db, f"NEW_{index}", None)
    assert len(db.get_local_files_missing_subtitle(limit=2)) == 2
