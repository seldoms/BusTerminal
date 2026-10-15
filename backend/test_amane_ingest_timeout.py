"""amane 入库超时处理单测（全部离线，不发网络请求）。

回归点：轮询超时的任务快照 status 常常还是 running/queued，不能被当成"没失败"
继续往下走；超时必须以失败处置，且 reason 要能看出是 timeout 而非普通失败。
"""

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from amane_ingest_service import AmaneIngestService


class _FakeDb:
    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self.config = dict(config or {})
        self.events: List[Dict[str, Any]] = []

    def get_config(self, key: str) -> Any:
        return self.config.get(key)

    def get_local_file(self, movie_id: str) -> None:
        return None

    def save_local_file_amane(self, movie_id: str, **kwargs: Any) -> bool:
        return True

    def save_local_file_jellyfin(self, movie_id: str, *, item_id: Optional[str] = None) -> bool:
        return True

    def record_lifecycle_event(self, **kwargs: Any) -> bool:
        self.events.append(kwargs)
        return True


class _FakeAmaneService:
    """内存版 amane：每个阶段返回指定的任务快照，记录被调用过的动作。"""

    def __init__(
        self,
        *,
        refresh: Optional[Dict[str, Any]] = None,
        scrape: Optional[Dict[str, Any]] = None,
        organize: Optional[Dict[str, Any]] = None,
        media: Optional[Dict[str, Any]] = None,
        libraries: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.tasks = {"refresh": refresh, "scrape": scrape, "organize": organize}
        self.media = media
        self.libraries = libraries if libraries is not None else [{"id": 1, "path": "/hp", "automation": "watch"}]
        self.calls: List[str] = []
        self._task_id = 0

    def list_libraries(self, **_: Any) -> List[Dict[str, Any]]:
        return self.libraries

    def find_media(self, number: str, **_: Any) -> Dict[str, Any]:
        self.calls.append("find_media")
        return {"items": [self.media] if self.media else []}

    def get_client(self) -> "_FakeAmaneService":
        """真实 AmaneService 暴露 get_client；入库回读/改号走它。"""
        return self

    def get_media(self, media_id: int) -> Dict[str, Any]:
        self.calls.append("get_media")
        return dict(self.media) if self.media else {}

    def patch_media(self, media_id: int, **fields: Any) -> Dict[str, Any]:
        self.calls.append("patch_media")
        if self.media:
            self.media.update(fields)
        return dict(self.media) if self.media else {}

    def _snapshot(self, name: str) -> Dict[str, Any]:
        self.calls.append(name)
        self._task_id += 1
        return dict(self.tasks[name] or {"id": self._task_id, "status": "done", "error": None})

    def trigger_refresh(self, library_id: int, *, path: str = "", **_: Any) -> Dict[str, Any]:
        return self._snapshot("refresh")

    def trigger_scrape(self, **_: Any) -> Dict[str, Any]:
        return self._snapshot("scrape")

    def trigger_organize(self, library_id: int, **_: Any) -> Dict[str, Any]:
        return self._snapshot("organize")


def _build(db: _FakeDb, amane: _FakeAmaneService) -> AmaneIngestService:
    return AmaneIngestService(
        db=db,
        amane_service=amane,
        logger=None,
        safe_exception_label=lambda exc: type(exc).__name__,
        jellyfin_client=None,
    )


def _failure_event(db: _FakeDb) -> Dict[str, Any]:
    return [e for e in db.events if e.get("event_type") == "amane_ingest" and e.get("execution_result") == "failed"][-1]


def test_task_outcome_classifies_status_and_timeout():
    classify = AmaneIngestService._task_outcome
    assert classify({"status": "done"}) == "done"
    assert classify({"status": "failed"}) == "failed"
    assert classify({"status": "running", "wait_timeout": True}) == "timeout"
    assert classify({"status": "running"}) == "running"
    # queued 超时同样算 timeout（status 还没进入终态）
    assert classify({"status": "queued", "wait_timeout": True}) == "timeout"
    # done/failed 是终态，即使带 wait_timeout 也不该被误判
    assert classify({"status": "done", "wait_timeout": True}) == "done"
    assert classify({"status": "failed", "wait_timeout": True}) == "failed"


def test_refresh_timeout_is_failure_not_success():
    db = _FakeDb()
    amane = _FakeAmaneService(
        refresh={"id": 1, "status": "running", "wait_timeout": True, "error": None},
        media={"id": 7, "path": "/hp/ABC-123.mp4", "status": "pending", "number": "ABC-123"},
    )
    trace = _build(db, amane).ingest("ABC-123")

    assert trace["organize_status"] == "failed"
    assert trace["error"]["stage"] == "refresh"
    assert trace["error"]["code"] == "timeout"
    # 超时后不再往下走：既不回读媒体，也不触发 organize
    assert [s["stage"] for s in trace["stages"]] == ["refresh"]
    assert "find_media" not in amane.calls
    assert "organize" not in amane.calls

    failed = _failure_event(db)
    assert failed["organize_status"] == "failed"
    assert str(failed["reason"]).startswith("refresh:")
    assert "timeout" in failed["reason"]


def test_scrape_timeout_stops_before_organize():
    db = _FakeDb()
    amane = _FakeAmaneService(
        refresh={"id": 1, "status": "done", "error": None},
        scrape={"id": 2, "status": "running", "wait_timeout": True, "error": None},
        media={"id": 7, "path": "/hp/ABC-123.mp4", "status": "pending", "number": "ABC-123"},
    )
    trace = _build(db, amane).ingest("ABC-123")

    assert trace["organize_status"] == "failed"
    assert [s["stage"] for s in trace["stages"]] == ["refresh", "scrape"]
    assert "organize" not in amane.calls
    assert "timeout" in _failure_event(db)["reason"]


def test_task_failed_still_distinct_from_timeout():
    """真失败仍然按 failed 处置（原行为），且 reason 不带 timeout。"""
    db = _FakeDb()
    amane = _FakeAmaneService(refresh={"id": 1, "status": "failed", "error": "refresh_boom"})
    trace = _build(db, amane).ingest("ABC-123")

    assert trace["organize_status"] == "failed"
    assert trace["error"]["stage"] == "refresh"
    assert "timeout" not in str(trace["error"]["code"])


def test_done_path_still_completes():
    """回归：正常 done 路径不受影响。"""
    db = _FakeDb()
    amane = _FakeAmaneService(
        media={"id": 7, "path": "/hp/ABC-123.mp4", "status": "pending", "size": 123, "number": "ABC-123"},
    )
    trace = _build(db, amane).ingest("ABC-123")

    assert trace["organize_status"] == "completed"
    assert [s["stage"] for s in trace["stages"]] == ["refresh", "scrape", "organize"]
    assert trace["media"]["id"] == 7
    assert any(e.get("event_type") == "amane_ingest_done" for e in db.events)
