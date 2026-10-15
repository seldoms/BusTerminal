"""入库闭环离线测试：假 amane + 假 DB，验证状态机与落库。"""
import unittest
from typing import Any, Dict, List, Optional

from amane_client import AmaneError, AmaneUnavailable
from amane_ingest_service import AmaneIngestService


class FakeDB:
    def __init__(self, config: Optional[Dict[str, Any]] = None, local_file: Optional[Dict] = None) -> None:
        self.config = dict(config or {})
        self.local_file = local_file
        self.saved: Optional[Dict[str, Any]] = None
        self.jellyfin: Optional[Dict[str, Any]] = None
        self.events: List[Dict[str, Any]] = []

    def get_config(self, key: str) -> Any:
        return self.config.get(key)

    def set_config(self, key: str, value: Any) -> None:
        self.config[key] = value

    def get_local_file(self, movie_id: str) -> Optional[Dict]:
        return self.local_file

    def save_local_file_amane(self, movie_id: str, **kwargs: Any) -> bool:
        self.saved = {"movie_id": movie_id, **kwargs}
        return True

    def save_local_file_jellyfin(self, movie_id: str, *, item_id: Optional[str] = None) -> bool:
        self.jellyfin = {"movie_id": movie_id, "item_id": item_id}
        return True

    def record_lifecycle_event(self, **kwargs: Any) -> bool:
        self.events.append(kwargs)
        return True


class FakeAmaneService:
    """内存版 amane：可配置库、媒体、任务结果。"""

    def __init__(
        self,
        *,
        libraries: Optional[List[Dict[str, Any]]] = None,
        media: Optional[Dict[str, Any]] = None,
        media_by_id: Optional[Dict[str, Any]] = None,
        task_status: str = "done",
        raise_on: Optional[str] = None,
        patch_error: bool = False,
        get_media_empty: bool = False,
    ) -> None:
        self.libraries = libraries if libraries is not None else [{"id": 1, "automation": "scrape"}]
        self.media = media
        # get_media 的返回值（默认与 find_media 同一条）；单独给用来模拟
        # 「按番号搜不到、但按 id 能拿到」——番号被 amane 改坏时就是这个情形。
        self.media_by_id = media_by_id if media_by_id is not None else media
        self.task_status = task_status
        self.raise_on = raise_on
        self.patch_error = patch_error
        self.get_media_empty = get_media_empty
        self.calls: List[str] = []
        self.refresh_paths: List[str] = []
        self.patched: List[Dict[str, Any]] = []
        self._task_id = 0

    def list_libraries(self, **_: Any) -> List[Dict[str, Any]]:
        return self.libraries

    def get_client(self) -> "FakeAmaneService":
        return self

    def get_media(self, media_id: int) -> Dict[str, Any]:
        self.calls.append(f"get:{media_id}")
        if self.get_media_empty:
            return {}
        return dict(self.media_by_id) if self.media_by_id else {}

    def patch_media(self, media_id: int, **fields: Any) -> Dict[str, Any]:
        self.calls.append(f"patch:{media_id}")
        self.patched.append({"media_id": media_id, **fields})
        if self.patch_error:
            raise AmaneError("patch_boom")
        if self.media_by_id and self.media_by_id.get("id") == media_id:
            self.media_by_id.update(fields)
        return dict(self.media_by_id) if self.media_by_id else {}

    def find_media(self, number: str, **_: Any) -> Dict[str, Any]:
        self.calls.append(f"find:{number}")
        return {"items": [self.media] if self.media else [], "total": 1 if self.media else 0}

    def _task(self, name: str) -> Dict[str, Any]:
        self.calls.append(name)
        if self.raise_on == name:
            raise AmaneError(f"{name}_boom")
        self._task_id += 1
        return {"id": self._task_id, "status": self.task_status, "error": None}

    def trigger_refresh(self, library_id: int, *, path: str = "", **_: Any) -> Dict[str, Any]:
        self.refresh_paths.append(path)
        return self._task("refresh")

    def trigger_scrape(self, **_: Any) -> Dict[str, Any]:
        return self._task("scrape")

    def trigger_organize(self, library_id: int, **_: Any) -> Dict[str, Any]:
        return self._task("organize")


class FakeJellyfinClient:
    """内存版 Jellyfin：`hit_after` 次查询后才返回结果，用来验证轮询而非一次命中。"""

    def __init__(self, items: Optional[List[Dict[str, Any]]] = None, hit_after: int = 1,
                 host: str = "http://jellyfin", api_key: str = "key") -> None:
        self.items = items if items is not None else []
        self.hit_after = hit_after
        self.host = host
        self.api_key = api_key
        self.searches = 0
        self.refreshed = 0

    def reload_config(self) -> None:
        return None

    def refresh_library(self) -> bool:
        self.refreshed += 1
        return True

    def search_items(self, term: str, limit: int = 5) -> List[Dict[str, Any]]:
        self.searches += 1
        return self.items if self.searches >= self.hit_after else []


def build(db: FakeDB, amane: FakeAmaneService, jellyfin: Any = None) -> AmaneIngestService:
    return AmaneIngestService(db=db, amane_service=amane, logger=None,
                              safe_exception_label=lambda exc: type(exc).__name__,
                              jellyfin_client=jellyfin)


class IngestTest(unittest.TestCase):
    def test_full_pipeline(self):
        db = FakeDB()
        amane = FakeAmaneService(media={"id": 7, "path": "/hp/ABC-123.mp4", "status": "pending",
                                        "size": 123, "number": "ABC-123"})
        trace = build(db, amane).ingest("abc-123")
        self.assertEqual(trace["organize_status"], "completed")
        self.assertEqual([s["stage"] for s in trace["stages"]], ["refresh", "scrape", "organize"])
        self.assertEqual(trace["media"]["id"], 7)
        self.assertEqual(db.saved["media_file_id"], 7)
        self.assertEqual(db.saved["file_path"], "/hp/ABC-123.mp4")
        self.assertIn("amane_ingest_done", [e["event_type"] for e in db.events])
        done = [e for e in db.events if e["event_type"] == "amane_ingest_done"][0]
        self.assertEqual(done["organize_status"], "completed")
        self.assertEqual(done["jellyfin_status"], "pending")

    def test_already_scraped_skips_scrape_stage(self):
        db = FakeDB()
        amane = FakeAmaneService(media={"id": 7, "path": "/hp/ABC-123.mp4", "status": "scraped"})
        trace = build(db, amane).ingest("ABC-123")
        self.assertEqual([s["stage"] for s in trace["stages"]], ["refresh", "organize"])
        self.assertNotIn("scrape", amane.calls)

    def test_idempotent_when_already_linked(self):
        db = FakeDB(local_file={"amane_media_file_id": 7})
        trace = build(db, FakeAmaneService()).ingest("ABC-123")
        self.assertEqual(trace["organize_status"], "skipped")
        self.assertEqual(trace["reason"], "already_linked")
        self.assertEqual(db.events, [])

    def test_force_reruns_when_linked(self):
        db = FakeDB(local_file={"amane_media_file_id": 7})
        amane = FakeAmaneService(media={"id": 7, "path": "/hp/ABC-123.mp4", "status": "scraped"})
        trace = build(db, amane).ingest("ABC-123", force=True)
        self.assertEqual(trace["organize_status"], "completed")

    def test_wrong_number_is_corrected(self):
        """amane 扫描 _incoming 时按原始文件名猜号，站点水印会被当成番号前缀。

        实测：`4k2.mestart-646.mp4` → `MESTART-646`（真番号 START-646）。
        路径是对的，只有 number 列错 —— 而 bus 全靠它对账，错了整条链就断了。
        """
        db = FakeDB()
        amane = FakeAmaneService(
            media={"id": 7, "path": "/hp/_incoming/start-646/4k2.mestart-646.mp4",
                   "status": "pending", "number": "MESTART-646"})
        trace = build(db, amane).ingest("START-646")
        self.assertEqual(trace["organize_status"], "completed")
        self.assertEqual(trace["media"]["number"], "START-646")
        self.assertIn({"media_id": 7, "number": "START-646"}, amane.patched)
        self.assertEqual(db.saved["file_path"], "/hp/_incoming/start-646/4k2.mestart-646.mp4")

    def test_correct_number_is_not_rewritten(self):
        db = FakeDB()
        amane = FakeAmaneService(media={"id": 7, "path": "/hp/ABC-123.mp4", "status": "scraped",
                                        "number": "ABC-123"})
        build(db, amane).ingest("abc-123")
        self.assertEqual(amane.patched, [])

    def test_reread_media_prefers_id(self):
        """番号被改坏后按番号搜不到，回读必须走 id，否则拿到的还是整理前的旧快照。"""
        amane = FakeAmaneService(media={"id": 7, "number": "WRONG", "path": "/hp/old.mp4"},
                                 media_by_id={"id": 7, "number": "WRONG",
                                              "path": "/hp/ABC-123/ABC-123.mp4"})
        latest = build(FakeDB(), amane)._reread_media(7, "ABC-123")
        self.assertEqual(latest["path"], "/hp/ABC-123/ABC-123.mp4")
        self.assertNotIn("find:ABC-123", amane.calls)

    def test_reread_media_falls_back_to_number_search(self):
        amane = FakeAmaneService(get_media_empty=True,
                                 media={"id": 7, "number": "ABC-123", "path": "/hp/ABC-123.mp4"})
        latest = build(FakeDB(), amane)._reread_media(7, "ABC-123")
        self.assertEqual(latest["id"], 7)
        self.assertIn("find:ABC-123", amane.calls)

    def test_number_patch_failure_does_not_break_ingest(self):
        db = FakeDB()
        amane = FakeAmaneService(patch_error=True,
                                 media={"id": 7, "path": "/hp/ABC-123.mp4", "status": "scraped",
                                        "number": "MEABC-123"})
        trace = build(db, amane).ingest("ABC-123")
        self.assertEqual(trace["organize_status"], "completed")
        self.assertEqual(trace["media"]["number"], "MEABC-123")

    def test_media_not_indexed(self):
        db = FakeDB()
        trace = build(db, FakeAmaneService(media=None)).ingest("ABC-123")
        self.assertEqual(trace["organize_status"], "not_found")
        self.assertEqual(trace["reason"], "media_not_indexed")
        pending = [e for e in db.events if e["event_type"] == "amane_ingest"][0]
        self.assertEqual(pending["organize_status"], "pending")

    def test_no_library(self):
        db = FakeDB()
        trace = build(db, FakeAmaneService(libraries=[])).ingest("ABC-123")
        self.assertEqual(trace["organize_status"], "failed")
        self.assertEqual(trace["reason"], "no_library")

    def test_disabled(self):
        db = FakeDB({"amane_ingest_enabled": "false"})
        trace = build(db, FakeAmaneService()).ingest("ABC-123")
        self.assertEqual(trace["reason"], "ingest_disabled")

    def test_task_failure_records_failed(self):
        db = FakeDB()
        amane = FakeAmaneService(task_status="failed")
        trace = build(db, amane).ingest("ABC-123")
        self.assertEqual(trace["organize_status"], "failed")
        self.assertEqual(trace["error"]["stage"], "refresh")
        failed = [e for e in db.events if e["event_type"] == "amane_ingest"][0]
        self.assertEqual(failed["organize_status"], "failed")
        self.assertEqual(failed["execution_result"], "failed")

    def test_client_error_is_contained(self):
        db = FakeDB()
        amane = FakeAmaneService(raise_on="refresh")
        trace = build(db, amane).ingest("ABC-123")
        self.assertEqual(trace["organize_status"], "failed")
        self.assertEqual(trace["error"]["stage"], "refresh")

    def test_organize_disabled_config(self):
        db = FakeDB({"amane_organize_enabled": "false"})
        amane = FakeAmaneService(media={"id": 7, "path": "/hp/ABC-123.mp4", "status": "scraped"})
        trace = build(db, amane).ingest("ABC-123")
        self.assertEqual([s["stage"] for s in trace["stages"]], ["refresh"])
        self.assertEqual(trace["organize_status"], "completed")

    def test_library_picks_watch_automation(self):
        db = FakeDB()
        amane = FakeAmaneService(libraries=[{"id": 9, "automation": "none"},
                                            {"id": 3, "automation": "watch"}])
        trace = build(db, amane).ingest("ABC-123")
        self.assertEqual(trace["library_id"], 3)

    def test_relative_scan_path_is_expanded_to_library_root(self):
        """amane 只接受库路径的绝对路径后代：_incoming 必须补成 /hp/_incoming。"""
        db = FakeDB({"amane_scan_path": "_incoming"})
        amane = FakeAmaneService(libraries=[{"id": 1, "path": "/hp", "automation": "watch"}],
                                 media={"id": 7, "path": "/hp/_incoming/ABC-123.mp4", "status": "scraped"})
        build(db, amane).ingest("ABC-123")
        self.assertEqual(amane.refresh_paths, ["/hp/_incoming"])

    def test_absolute_scan_path_is_kept_as_is(self):
        db = FakeDB({"amane_scan_path": "/hp/_incoming"})
        amane = FakeAmaneService(libraries=[{"id": 1, "path": "/hp", "automation": "watch"}],
                                 media={"id": 7, "path": "/hp/_incoming/ABC-123.mp4", "status": "scraped"})
        build(db, amane).ingest("ABC-123")
        self.assertEqual(amane.refresh_paths, ["/hp/_incoming"])

    def test_batch_summary(self):
        db = FakeDB()
        amane = FakeAmaneService(media={"id": 7, "path": "/hp/ABC-123.mp4", "status": "scraped"})
        summary = build(db, amane).ingest_many([
            {"movie_id": "ABC-123"},
            {"movie_id": "DEF-456"},
        ])
        # 两片共用同一个假 media，因此都判为入库成功（演练语义：按番号命中）
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["completed"] + summary["not_found"], 2)

    def test_config_roundtrip(self):
        db = FakeDB()
        service = build(db, FakeAmaneService())
        service.save_config(enabled=False, library_id=3, scan_path="completed",
                            organize_enabled=True, verify_visible=False)
        self.assertEqual(db.get_config("amane_ingest_enabled"), "false")
        self.assertEqual(db.get_config("amane_library_id"), 3)
        self.assertEqual(db.get_config("amane_ingest_verify_visible"), "false")
        self.assertEqual(service.get_config(), {
            "enabled": False, "library_id": 3, "scan_path": "completed",
            "organize_enabled": True, "verify_visible": False, "dedupe_enabled": True,
        })

    def test_visibility_confirmed(self):
        db = FakeDB()
        jellyfin = FakeJellyfinClient(items=[{"Id": "item-9", "Name": "ABC-123 某片"}], hit_after=2)
        result = build(db, FakeAmaneService(), jellyfin).confirm_visibility(
            "abc-123", timeout=5, interval=0.01
        )
        self.assertEqual(result["status"], "visible")
        self.assertEqual(result["item_id"], "item-9")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(jellyfin.refreshed, 1)
        self.assertEqual(db.jellyfin["item_id"], "item-9")
        self.assertEqual(db.events[-1]["jellyfin_status"], "visible")

    def test_visibility_timeout(self):
        db = FakeDB()
        result = build(db, FakeAmaneService(), FakeJellyfinClient(items=[])).confirm_visibility(
            "abc-123", timeout=0.05, interval=0.01
        )
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["reason"], "jellyfin_not_visible")
        self.assertIsNone(db.jellyfin)
        self.assertEqual(db.events[-1]["jellyfin_status"], "pending")

    def test_visibility_skipped_when_unconfigured(self):
        db = FakeDB()
        result = build(db, FakeAmaneService(), FakeJellyfinClient(host="", api_key="")).confirm_visibility(
            "abc-123", timeout=1, interval=0.01
        )
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "jellyfin_not_configured")
        self.assertEqual(db.events, [])

    def test_ingest_runs_visibility_when_enabled(self):
        db = FakeDB()
        amane = FakeAmaneService(media={"id": 7, "path": "/hp/ABC-123.mp4", "status": "scraped",
                                        "size": 1, "number": "ABC-123"})
        jellyfin = FakeJellyfinClient(items=[{"Id": "item-9", "Name": "ABC-123"}])
        trace = build(db, amane, jellyfin).ingest("abc-123")
        self.assertEqual(trace["organize_status"], "completed")
        self.assertEqual((trace["visibility"] or {}).get("status"), "visible")

    def test_ingest_skips_visibility_when_disabled(self):
        db = FakeDB(config={"amane_ingest_verify_visible": "false"})
        amane = FakeAmaneService(media={"id": 7, "path": "/hp/ABC-123.mp4", "status": "scraped",
                                        "size": 1, "number": "ABC-123"})
        trace = build(db, amane, FakeJellyfinClient()).ingest("abc-123")
        self.assertIsNone(trace["visibility"])


class LibraryPresenceTest(unittest.TestCase):
    def test_present_in_amane_backfills_identity(self):
        db = FakeDB()
        amane = FakeAmaneService(media={"id": 42, "path": "/hp/ABC-123.mp4",
                                        "status": "scraped", "size": 999, "number": "ABC-123"})
        result = build(db, amane).check_library_presence("abc-123")
        self.assertTrue(result["checked"])
        self.assertTrue(result["present"])
        self.assertEqual(result["sources"], ["amane"])
        self.assertEqual(db.saved["media_file_id"], 42)
        self.assertEqual(db.saved["file_size"], 999)

    def test_absent_everywhere(self):
        result = build(FakeDB(), FakeAmaneService()).check_library_presence("ABC-123")
        self.assertTrue(result["checked"])
        self.assertFalse(result["present"])
        self.assertEqual(result["sources"], [])

    def test_jellyfin_only_counts_as_present(self):
        db = FakeDB()
        jellyfin = FakeJellyfinClient(items=[{"Id": "item-9", "Name": "ABC-123"}])
        result = build(db, FakeAmaneService(), jellyfin).check_library_presence("ABC-123")
        self.assertTrue(result["present"])
        self.assertEqual(result["sources"], ["jellyfin"])
        self.assertEqual(db.jellyfin["item_id"], "item-9")

    def test_amane_unavailable_does_not_block_download(self):
        class Boom:
            def find_media(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
                raise AmaneUnavailable("amane 没起")

        result = build(FakeDB(), Boom()).check_library_presence("ABC-123")
        self.assertFalse(result["checked"], "查不动就得承认没查过")
        self.assertFalse(result["present"], "查不到≠没有，不能据此拦下载")
        self.assertEqual(result["error"], "amane_unavailable")

    def test_disabled_by_config(self):
        db = FakeDB(config={"amane_dedupe_enabled": "false"})
        result = build(db, FakeAmaneService()).check_library_presence("ABC-123")
        self.assertEqual(result["error"], "dedupe_disabled")
        self.assertFalse(result["present"])

    def test_missing_number_short_circuits(self):
        result = build(FakeDB(), FakeAmaneService()).check_library_presence("")
        self.assertEqual(result["error"], "missing_number")

    def test_config_roundtrip(self):
        db = FakeDB()
        service = build(db, FakeAmaneService())
        self.assertTrue(service.get_config()["dedupe_enabled"])
        service.save_config(dedupe_enabled=False)
        self.assertFalse(service.get_config()["dedupe_enabled"])
        self.assertEqual(db.get_config("amane_dedupe_enabled"), "false")

    def test_failure_reason_carries_readable_error(self):
        """列表页只暴露 last_reason：原因必须包含可读报错，不能只有阶段名。"""
        db = FakeDB()
        amane = FakeAmaneService(raise_on="refresh")
        trace = build(db, amane).ingest("ABC-123")
        self.assertEqual(trace["organize_status"], "failed")
        failed = [e for e in db.events if e["event_type"] == "amane_ingest"][-1]
        self.assertTrue(str(failed["reason"]).startswith("refresh:"), failed["reason"])


if __name__ == "__main__":
    unittest.main()
