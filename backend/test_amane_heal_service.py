"""amane 入库自愈单测（全部离线，不发网络请求、不连数据库）。"""

import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from amane_client import AmaneError
from amane_heal_service import AmaneHealService


class _FakeDb:
    def __init__(self, rows=None, config=None):
        self.rows = list(rows or [])
        self.config = dict(config or {})
        self.events = []

    def get_movie_lifecycle(self, movie_id=None, *, organize_status=None, limit=100, offset=0, **kwargs):
        out = [r for r in self.rows if not organize_status or r.get("organize_status") == organize_status]
        return out[offset:offset + limit]

    def get_config(self, key):
        return self.config.get(key)

    def record_lifecycle_event(self, **kwargs):
        self.events.append(kwargs)
        return True


class _FakeClient:
    """内存版 amane HTTP 客户端，记录所有写调用。"""

    def __init__(self, media=None, scrape_status="done", organize_status="done",
                 patch_error=None, scrape_error=None):
        self.media = media if media is not None else []
        self.scrape_status = scrape_status
        self.organize_status = organize_status
        self.patch_error = patch_error
        self.scrape_error = scrape_error
        self.calls = []
        self.patches = []
        self._task_id = 0
        self._last_task = None

    def list_media(self, *, search=None, limit=20, **kwargs):
        self.calls.append(("list_media", search))
        wanted = str(search or "").upper()
        items = [m for m in self.media if not wanted or wanted in str(m.get("path") or "").upper()]
        return {"items": items[:limit], "total": len(items)}

    def patch_media(self, media_id, **fields):
        self.calls.append(("patch_media", media_id))
        if self.patch_error:
            raise self.patch_error
        self.patches.append((media_id, fields))
        return {"id": media_id, **fields}

    def _task(self, status):
        self._task_id += 1
        self._last_task = {"id": self._task_id, "status": status, "error": None}
        return self._last_task

    def submit_scrape(self, **kwargs):
        self.calls.append(("submit_scrape", kwargs))
        if self.scrape_error:
            raise self.scrape_error
        return self._task(self.scrape_status)

    def submit_organize(self, library_id, *, media_file_ids=None, **kwargs):
        self.calls.append(("submit_organize", library_id, tuple(media_file_ids or ())))
        return self._task(self.organize_status)

    def wait_for_task(self, task_id, **kwargs):
        self.calls.append(("wait_for_task", task_id))
        return self._last_task


class _FakeAmaneService:
    def __init__(self, client=None, libraries=None):
        self._client = client
        self.libraries = libraries if libraries is not None else [{"id": 1, "automation": "watch"}]

    def get_client(self):
        return self._client

    def list_libraries(self):
        return self.libraries


class _FakeJellyfinClient:
    """只关心有没有被要求重扫库（刷不刷得动由 Jellyfin 自己负责）。"""

    def __init__(self, error=None):
        self.refresh_calls = 0
        self.reloads = 0
        self.error = error

    def reload_config(self):
        self.reloads += 1

    def refresh_library(self):
        self.refresh_calls += 1
        if self.error:
            raise self.error


def _build(db, client, libraries=None, jellyfin=None):
    return AmaneHealService(
        db=db,
        amane_service=_FakeAmaneService(client, libraries),
        amane_client=client,
        jellyfin_client=jellyfin,
        logger=None,
        safe_exception_label=lambda exc: type(exc).__name__,
        display_value_for_log=lambda value: str(value),
        config=None,
    )


def _pending_media(number=None):
    return {
        "id": 95,
        "number": number,
        "status": "pending",
        "path": "/hp/all/483DAM-062 尋井うみ/483DAM-062.mp4",
    }


# ---------- detect_stuck ----------

def test_detect_stuck_filters_by_age_and_sorts_desc():
    now = time.time()
    db = _FakeDb([
        {"movie_id": "A", "title": "a", "organize_status": "pending",
         "jellyfin_status": "unknown", "updated_at": now - 10000},
        {"movie_id": "B", "title": "b", "organize_status": "pending",
         "jellyfin_status": "unknown", "updated_at": now - 100},
        {"movie_id": "C", "title": "c", "organize_status": "failed",
         "jellyfin_status": "unknown", "updated_at": now - 9000},
        {"movie_id": "D", "title": "d", "organize_status": "not_found",
         "jellyfin_status": "unknown", "updated_at": now - 300},
    ])
    stuck = _build(db, _FakeClient()).detect_stuck(limit=20, older_than_seconds=7200)

    assert [row["movie_id"] for row in stuck] == ["A", "C"]
    assert stuck[0]["stuck_seconds"] > stuck[1]["stuck_seconds"]
    assert set(stuck[0]) == {"movie_id", "title", "organize_status", "jellyfin_status",
                             "updated_at", "stuck_seconds"}


def test_detect_stuck_respects_limit():
    now = time.time()
    rows = [
        {"movie_id": f"M{i}", "title": str(i), "organize_status": "pending",
         "jellyfin_status": "unknown", "updated_at": now - 10000 - i}
        for i in range(5)
    ]
    stuck = _build(_FakeDb(rows), _FakeClient()).detect_stuck(limit=2, older_than_seconds=7200)
    assert len(stuck) == 2


# ---------- heal_one ----------

def test_heal_one_success_backfills_number_and_records_completed():
    db = _FakeDb()
    client = _FakeClient(media=[_pending_media(number=None)])
    outcome = _build(db, client).heal_one("483DAM-062", library_id=1)

    assert outcome["healed"] is True
    assert outcome["stage"] == "completed"
    assert outcome["media_file_id"] == 95
    assert [s["stage"] for s in outcome["stages"]] == ["patch_media", "scrape", "organize"]
    assert client.patches == [(95, {"number": "483DAM-062"})]
    assert db.events[0]["organize_status"] == "completed"


def test_heal_one_patches_only_when_number_differs():
    """番号对上就不动；空了或错了才改。

    注意"非空"不等于"正确"：amane 扫描 _incoming 时会把站点水印当番号前缀
    （`4k2.mestart-646.mp4` → MESTART-646），所以只判空是不够的。
    """
    db = _FakeDb()
    client = _FakeClient(media=[_pending_media(number="483DAM-062")])
    outcome = _build(db, client).heal_one("483DAM-062", library_id=1)

    assert outcome["healed"] is True
    assert client.patches == []
    assert [s["stage"] for s in outcome["stages"]] == ["scrape", "organize"]


def test_heal_one_rewrites_wrong_number():
    db = _FakeDb()
    client = _FakeClient(media=[_pending_media(number="ME483DAM-062")])
    outcome = _build(db, client).heal_one("483DAM-062", library_id=1)

    assert outcome["healed"] is True
    assert client.patches == [(95, {"number": "483DAM-062"})]
    assert [s["stage"] for s in outcome["stages"]] == ["patch_media", "scrape", "organize"]


def test_heal_one_media_not_found():
    db = _FakeDb()
    client = _FakeClient(media=[{"id": 1, "number": None, "status": "scraped",
                                 "path": "/hp/other/OTHER-001.mp4"}])
    outcome = _build(db, client).heal_one("483DAM-062")

    assert outcome["healed"] is False
    assert outcome["stage"] == "find_media"
    assert outcome["reason"] == "media_not_found"
    assert outcome["media_file_id"] is None
    assert client.patches == []
    assert ("submit_scrape", {}) not in client.calls
    # 失败也要记一笔：靠它刷新 movie_lifecycle.updated_at，让巡检在阈值窗口内
    # 不再反复检出同一条（否则每轮都白查一次 amane）。
    assert len(db.events) == 1
    assert db.events[0]["reason"] == "media_not_found"
    assert db.events[0]["organize_status"] == "pending"


def test_heal_one_scrape_failure_stops_before_organize():
    db = _FakeDb()
    client = _FakeClient(media=[_pending_media(number="483DAM-062")], scrape_status="failed")
    outcome = _build(db, client).heal_one("483DAM-062", library_id=1)

    assert outcome["healed"] is False
    assert outcome["stage"] == "scrape"
    assert not any(call[0] == "submit_organize" for call in client.calls)
    assert db.events[0]["organize_status"] == "failed"


def test_heal_one_scrape_raises_amane_error():
    db = _FakeDb()
    client = _FakeClient(media=[_pending_media(number="483DAM-062")],
                         scrape_error=AmaneError("scrape_boom"))
    outcome = _build(db, client).heal_one("483DAM-062", library_id=1)

    assert outcome["healed"] is False
    assert outcome["stage"] == "scrape"
    assert outcome["reason"] == "amane_error"


# ---------- heal_batch ----------

def test_heal_batch_dry_run_does_not_write():
    now = time.time()
    db = _FakeDb([
        {"movie_id": "A", "title": "a", "organize_status": "pending",
         "jellyfin_status": "unknown", "updated_at": now - 10000},
        {"movie_id": "B", "title": "b", "organize_status": "failed",
         "jellyfin_status": "unknown", "updated_at": now - 9000},
    ])
    client = _FakeClient(media=[_pending_media(number=None)])
    report = _build(db, client).heal_batch(limit=10, older_than_seconds=7200, dry_run=True)

    assert report["dry_run"] is True
    assert report["scanned"] == 2
    assert report["attempted"] == 0
    assert report["healed"] == 0
    assert report["failed"] == 0
    assert {row["movie_id"] for row in report["would_heal"]} == {"A", "B"}
    assert client.calls == []
    assert db.events == []


def test_heal_batch_executes_when_not_dry_run():
    now = time.time()
    db = _FakeDb([
        {"movie_id": "483DAM-062", "title": "t", "organize_status": "pending",
         "jellyfin_status": "unknown", "updated_at": now - 10000},
    ])
    client = _FakeClient(media=[_pending_media(number="483DAM-062")])
    report = _build(db, client).heal_batch(limit=10, older_than_seconds=7200, dry_run=False)

    assert report["dry_run"] is False
    assert report["attempted"] == 1
    assert report["healed"] == 1
    assert report["failed"] == 0
    assert report["results"][0]["media_file_id"] == 95


# ---------- Jellyfin 刷新（2026-10-17 实测缺这一步：文件已整理但 Jellyfin 仍指向 _incoming） ----------

def test_heal_one_refreshes_jellyfin_after_organize():
    db = _FakeDb()
    client = _FakeClient(media=[_pending_media(number="483DAM-062")])
    jellyfin = _FakeJellyfinClient()
    outcome = _build(db, client, jellyfin=jellyfin).heal_one("483DAM-062", library_id=1)

    assert outcome["healed"] is True
    assert jellyfin.refresh_calls == 1


def test_heal_batch_refreshes_jellyfin_only_once_for_whole_batch():
    now = time.time()
    db = _FakeDb([
        {"movie_id": "483DAM-062", "title": "a", "organize_status": "pending",
         "jellyfin_status": "unknown", "updated_at": now - 10000},
        {"movie_id": "483DAM-063", "title": "b", "organize_status": "pending",
         "jellyfin_status": "unknown", "updated_at": now - 9000},
    ])
    media = [
        _pending_media(number="483DAM-062"),
        {"id": 96, "number": "483DAM-063", "status": "pending",
         "path": "/hp/all/483DAM-063/483DAM-063.mp4"},
    ]
    jellyfin = _FakeJellyfinClient()
    report = _build(db, _FakeClient(media=media), jellyfin=jellyfin).heal_batch(
        limit=10, older_than_seconds=7200, dry_run=False)

    assert report["healed"] == 2
    assert jellyfin.refresh_calls == 1
    assert report["jellyfin_refresh"] is True


def test_heal_batch_does_not_refresh_when_nothing_healed():
    now = time.time()
    db = _FakeDb([
        {"movie_id": "X-1", "title": "x", "organize_status": "pending",
         "jellyfin_status": "unknown", "updated_at": now - 10000},
    ])
    jellyfin = _FakeJellyfinClient()
    report = _build(db, _FakeClient(media=[]), jellyfin=jellyfin).heal_batch(
        limit=10, older_than_seconds=7200, dry_run=False)

    assert report["healed"] == 0
    assert jellyfin.refresh_calls == 0
    assert "jellyfin_refresh" not in report


def test_jellyfin_refresh_error_does_not_fail_heal():
    now = time.time()
    db = _FakeDb([
        {"movie_id": "483DAM-062", "title": "t", "organize_status": "pending",
         "jellyfin_status": "unknown", "updated_at": now - 10000},
    ])
    client = _FakeClient(media=[_pending_media(number="483DAM-062")])
    jellyfin = _FakeJellyfinClient(error=RuntimeError("jellyfin_down"))
    report = _build(db, client, jellyfin=jellyfin).heal_batch(
        limit=10, older_than_seconds=7200, dry_run=False)

    assert report["healed"] == 1
    assert report["jellyfin_refresh"] is False
