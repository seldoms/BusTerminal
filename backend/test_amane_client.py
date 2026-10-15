"""amane 对接层离线测试：不依赖网络，全部用假 session。"""
import re
import threading
import unittest
from typing import Any, Dict, List, Optional

import requests

import amane_client
from amane_client import (
    AmaneApiError,
    AmaneAuthError,
    AmaneClient,
    AmaneNotConfigured,
    AmaneUnavailable,
)
from amane_service import (
    AD_BLACKLIST_HINTS,
    AD_BLACKLIST_KEYWORDS,
    AmaneService,
    review_library,
)


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or ("" if payload is None else str(payload))

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """记录调用并按脚本返回响应。"""

    def __init__(self, responses: List[FakeResponse], raise_exc: bool = False) -> None:
        self.responses = list(responses)
        self.raise_exc = raise_exc
        self.calls: List[Dict[str, Any]] = []

    def request(self, method, url, *, params=None, json=None, headers=None, timeout=None):
        self.calls.append({
            "method": method,
            "url": url,
            "params": params or {},
            "json": json,
            "headers": headers or {},
            "timeout": timeout,
        })
        if self.raise_exc:
            raise requests.exceptions.ConnectionError("boom")
        if self.responses:
            return self.responses.pop(0)
        return FakeResponse(200, {})


def make_client(responses, **kwargs) -> tuple:
    session = FakeSession(responses)
    client = AmaneClient("http://amane.test:8000", "tok", session=session, **kwargs)
    return client, session


class AmaneClientTest(unittest.TestCase):
    def test_health(self):
        client, session = make_client([FakeResponse(200, {"status": "ok", "version": "0.18.0"})])
        self.assertEqual(client.health()["version"], "0.18.0")
        self.assertEqual(session.calls[0]["url"], "http://amane.test:8000/api/health")
        self.assertEqual(session.calls[0]["headers"]["Authorization"], "Bearer tok")

    def test_no_token_omits_auth_header(self):
        session = FakeSession([FakeResponse(200, {"status": "ok"})])
        client = AmaneClient("http://amane.test:8000", "", session=session)
        client.health()
        self.assertNotIn("Authorization", session.calls[0]["headers"])

    def test_missing_base_url(self):
        client = AmaneClient("", "tok")
        with self.assertRaises(AmaneNotConfigured):
            client.health()

    def test_401_maps_to_auth_error(self):
        client, _ = make_client([FakeResponse(401, {"detail": "缺少或无效的 API token."})])
        with self.assertRaises(AmaneAuthError):
            client.list_libraries()

    def test_500_maps_to_api_error(self):
        client, _ = make_client([FakeResponse(500, {"detail": "boom"})])
        with self.assertRaises(AmaneApiError) as ctx:
            client.list_libraries()
        self.assertEqual(ctx.exception.status_code, 500)

    def test_network_error_maps_to_unavailable(self):
        session = FakeSession([], raise_exc=True)
        client = AmaneClient("http://amane.test:8000", "tok", session=session)
        with self.assertRaises(AmaneUnavailable):
            client.health()

    def test_list_wraps_items(self):
        client, _ = make_client([FakeResponse(200, {"items": [{"id": 1, "name": "hp"}], "total": 1})])
        libs = client.list_libraries()
        self.assertEqual(len(libs), 1)
        self.assertEqual(libs[0]["name"], "hp")

    def test_list_media_params(self):
        client, session = make_client([FakeResponse(200, {"items": [], "total": 0})])
        client.list_media(library_id=3, status="pending", search="ABC-123", limit=10)
        self.assertEqual(session.calls[0]["params"], {
            "library_id": 3, "status": "pending", "search": "ABC-123", "limit": 10, "offset": 0,
        })

    def test_submit_refresh_payload(self):
        client, session = make_client([FakeResponse(202, {"id": 7, "status": "queued"})])
        task = client.submit_refresh(3, path="/hp/completed")
        self.assertEqual(task["id"], 7)
        # scrape 默认空：方案 C 下刮削只由 BusTerminal 显式提交的任务触发，
        # 沿用 amane 的 ["pending"] 会把库里所有待刮条目一次性丢进刮削队列。
        self.assertEqual(session.calls[0]["json"], {
            "type": "refresh",
            "library_id": 3,
            "path": "/hp/completed",
            "scan": ["add"],
            "scrape": [],
        })

    def test_submit_refresh_can_opt_into_scrape(self):
        client, session = make_client([FakeResponse(202, {"id": 7, "status": "queued"})])
        client.submit_refresh(3, path="/hp/completed", scrape=["pending"])
        self.assertEqual(session.calls[0]["json"]["scrape"], ["pending"])

    def test_submit_organize_prefers_media_ids(self):
        client, session = make_client([FakeResponse(202, {"id": 8})])
        client.submit_organize(3, media_file_ids=[11, 12])
        self.assertEqual(session.calls[0]["json"], {
            "type": "organize", "library_id": 3, "media_file_ids": [11, 12],
        })

    def test_submit_scrape_payload(self):
        client, session = make_client([FakeResponse(202, {"id": 9})])
        client.submit_scrape(number="ABC-123")
        self.assertEqual(session.calls[0]["json"], {"type": "scrape", "number": "ABC-123"})

    def test_wait_for_task_until_done(self):
        client, _ = make_client([
            FakeResponse(200, {"id": 5, "status": "running"}),
            FakeResponse(200, {"id": 5, "status": "done"}),
        ])
        slept: List[float] = []
        original_sleep = amane_client.time.sleep
        amane_client.time.sleep = lambda s: slept.append(s)
        try:
            snapshot = client.wait_for_task(5, timeout=10, interval=1)
        finally:
            amane_client.time.sleep = original_sleep
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(slept, [1])

    def test_wait_for_task_timeout_flagged(self):
        client, _ = make_client([
            FakeResponse(200, {"id": 5, "status": "running"}),
            FakeResponse(200, {"id": 5, "status": "running"}),
        ])
        original_sleep = amane_client.time.sleep
        amane_client.time.sleep = lambda s: None
        try:
            snapshot = client.wait_for_task(5, timeout=0, interval=1)
        finally:
            amane_client.time.sleep = original_sleep
        self.assertTrue(snapshot.get("wait_timeout"))

    def test_wait_for_task_respects_stop_event(self):
        client, _ = make_client([FakeResponse(200, {"id": 5, "status": "running"})])
        stop = threading.Event()
        stop.set()
        self.assertEqual(client.wait_for_task(5, stop_event=stop)["status"], "running")


class FakeDB:
    def __init__(self, values: Optional[Dict[str, Any]] = None) -> None:
        self.values = dict(values or {})

    def get_config(self, key: str) -> Any:
        return self.values.get(key)

    def set_config(self, key: str, value: Any) -> None:
        self.values[key] = value


class ReviewLibraryTest(unittest.TestCase):
    def test_healthy_library_has_no_blocking_suggestion(self):
        library = {
            "id": 1, "name": "hp", "path": "/hp", "automation": "watch", "ingest": "native",
            "recursive": True, "min_file_size": 200 * 1024 * 1024,
            "blacklist_patterns": list(AD_BLACKLIST_HINTS),
            "write_nfo": True,  # 方案 C：NFO 由 amane 写，BusTerminal 侧靠 Organizer 闸门不写
        }
        suggestions = review_library(library)
        self.assertEqual([s for s in suggestions if s["severity"] != "info"], [])

    def test_write_nfo_disabled_is_flagged(self):
        """关掉 write_nfo 会让 Jellyfin 拿不到 NFO 元数据，必须提示打开。"""
        suggestions = review_library({"automation": "watch", "write_nfo": False})
        suggestion = [s for s in suggestions if s["field"] == "write_nfo"][0]
        self.assertEqual(suggestion["recommended"], True)
        self.assertEqual(suggestion["severity"], "warn")

    def test_scrape_automation_is_blocking(self):
        """scrape 会自己刮削，与 BusTerminal 发起的任务抢跑 —— 视为阻塞项。"""
        suggestions = review_library({"automation": "scrape"})
        automation = [s for s in suggestions if s["field"] == "automation"][0]
        self.assertEqual(automation["severity"], "fail")
        self.assertEqual(automation["recommended"], "watch")

    def test_none_automation_is_only_info(self):
        """none 只是全靠我们 refresh，不阻塞。"""
        suggestions = review_library({"automation": "none"})
        automation = [s for s in suggestions if s["field"] == "automation"][0]
        self.assertEqual(automation["severity"], "info")

    def test_blacklist_hints_are_regex_not_glob(self):
        """amane 的 blacklist_patterns 是正则，glob 写法会被 422 拒掉。"""
        for hint in AD_BLACKLIST_HINTS:
            self.assertFalse(hint.startswith("*."), f"{hint} 仍是 glob 写法")
            re.compile(hint)
        self.assertEqual(
            len(AD_BLACKLIST_KEYWORDS), len(AD_BLACKLIST_HINTS),
            "关键词表与规则表必须一一对应",
        )

    def test_small_min_size_and_missing_blacklist(self):
        suggestions = review_library({
            "automation": "scrape", "min_file_size": 1024, "blacklist_patterns": [], "recursive": False,
        })
        fields = {s["field"] for s in suggestions}
        self.assertIn("min_file_size", fields)
        self.assertIn("blacklist_patterns", fields)
        self.assertIn("recursive", fields)


class AmaneServiceTest(unittest.TestCase):
    def test_settings_from_db_override_env(self):
        service = AmaneService(db=FakeDB({"amane_base_url": "http://db.test:8000/"}), logger=None)
        settings = service.get_settings()
        self.assertEqual(settings["base_url"], "http://db.test:8000")
        self.assertEqual(settings["token_source"], "")

    def test_save_settings_masks_token(self):
        db = FakeDB()
        service = AmaneService(db=db, logger=None)
        service.save_settings(base_url="http://x:8000", token="abcdefghijklmnop")
        self.assertEqual(db.get_config("amane_token"), "abcdefghijklmnop")
        settings = service.get_settings()
        self.assertTrue(settings["token_masked"].startswith("abcd"))
        self.assertNotIn("ijklmnop", settings["token_masked"])
        # 对外视图不得带出明文
        self.assertNotIn("abcdefghijklmnop", str(settings))

    def test_reload_config_rebuilds_client(self):
        db = FakeDB()
        service = AmaneService(db=db, logger=None)
        service.save_settings(base_url="http://x:8000", token="t1")
        first = service.get_client()
        service.save_settings(base_url="http://x:8000", token="t2")
        self.assertIsNot(first, service.get_client())

    def test_status_without_token_reports_not_authenticated(self):
        db = FakeDB({"amane_base_url": "http://x:8000"})
        service = AmaneService(db=db, logger=None)
        service._client = AmaneClient(
            "http://x:8000", "", session=FakeSession([FakeResponse(200, {"status": "ok", "version": "0.18.0"})])
        )
        service._client_key = ("http://x:8000", "")
        status = service.get_status()
        self.assertTrue(status["reachable"])
        self.assertFalse(status["authenticated"])
        self.assertFalse(status["configured"])

    def test_inventory_summary(self):
        db = FakeDB({"amane_base_url": "http://x:8000", "amane_token": "tok"})
        service = AmaneService(db=db, logger=None)
        service._client = AmaneClient(
            "http://x:8000", "tok",
            session=FakeSession([FakeResponse(200, {"items": [
                {"id": 1, "name": "hp", "path": "/hp", "automation": "none", "min_file_size": 0},
                {"id": 2, "name": "all1", "path": "/all1", "automation": "scrape",
                 "min_file_size": 200 * 1024 * 1024,
                 "blacklist_patterns": ["*.url", "*广告*", "*sample*", "*.part"], "recursive": True},
            ]})]),
        )
        service._client_key = ("http://x:8000", "tok")
        inventory = service.get_inventory()
        self.assertEqual(inventory["summary"]["total"], 2)
        self.assertEqual(inventory["summary"]["blocking"], 1)
        self.assertEqual(inventory["libraries"][0]["suggestions"][0]["field"], "automation")


if __name__ == "__main__":
    unittest.main()
