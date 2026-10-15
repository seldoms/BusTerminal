"""
Resilience & Architecture Verification Test Script (Milestone 3)
"""
import sys
import os
import time
import asyncio
import unittest
from unittest.mock import MagicMock, patch

# Ensure backend directory is in python path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import requests
import qb_api
from qb_service import QBService
from jellyfin_service import JellyfinService
import main


class TestResilienceAndArchitecture(unittest.TestCase):

    def test_qb_session_reuse(self):
        """Test QBService reuses authenticated requests.Session and only re-logins on invalidation."""
        mock_db = MagicMock()
        mock_db.get_config.return_value = {
            "host": "http://127.0.0.1",
            "port": 8080,
            "username": "admin",
            "password": "password"
        }
        mock_qb_api = MagicMock()
        fake_session = requests.Session()
        mock_qb_api.login.return_value = fake_session
        mock_config = MagicMock()
        mock_config.QB_HOST = "http://127.0.0.1"
        mock_config.QB_PORT = 8080

        service = QBService(
            db=mock_db,
            qb_api=mock_qb_api,
            config=mock_config,
            logger=MagicMock(),
            safe_number=float,
            format_bytes=str,
            format_speed=str,
            display_value_for_log=str,
            normalize_qb_root_path=lambda x: str(x),
            normalize_qb_hashes=lambda x: [x] if isinstance(x, str) else list(x or []),
            normalize_qb_category=lambda x: str(x),
            run_jav_file_filter_once=lambda: {},
            control_all_confirmation="CONFIRM",
        )

        # Call 1: get_session should execute qb_api.login once
        sess1, url1 = service.get_session()
        self.assertIsNotNone(sess1)
        self.assertEqual(url1, "http://127.0.0.1:8080")
        self.assertEqual(mock_qb_api.login.call_count, 1)

        # Call 2 & 3: should reuse existing sess1 without calling qb_api.login again
        sess2, url2 = service.get_session()
        sess3, url3 = service.get_session()
        self.assertIs(sess1, sess2)
        self.assertIs(sess2, sess3)
        self.assertEqual(mock_qb_api.login.call_count, 1)

        # Session invalidation (e.g. 401/403 or config change)
        service.invalidate_session()
        sess4, _ = service.get_session()
        self.assertEqual(mock_qb_api.login.call_count, 2)
        print("[PASS] qB session reuse and invalidation re-login verified successfully")

    def test_qb_circuit_breaker_fast_fail(self):
        """Test QBService circuit breaker sets 60s cooldown and returns degraded payload <1ms on unreachable host."""
        mock_db = MagicMock()
        mock_db.get_config.return_value = {
            "host": "http://127.0.0.1",
            "port": 9999,  # Unreachable port
            "username": "admin",
            "password": "password"
        }
        mock_qb_api = MagicMock()
        # Simulate connection timeout during login
        mock_qb_api.login.side_effect = requests.exceptions.ConnectTimeout("Connection timeout to 127.0.0.1:9999")
        mock_config = MagicMock()

        service = QBService(
            db=mock_db,
            qb_api=mock_qb_api,
            config=mock_config,
            logger=MagicMock(),
            safe_number=float,
            format_bytes=str,
            format_speed=str,
            display_value_for_log=str,
            normalize_qb_root_path=lambda x: str(x),
            normalize_qb_hashes=lambda x: [x] if isinstance(x, str) else list(x or []),
            normalize_qb_category=lambda x: str(x),
            run_jav_file_filter_once=lambda: {},
            control_all_confirmation="CONFIRM",
        )

        # Call 1: Connection fails, sets circuit breaker open (OFFLINE state for 60s)
        res1 = service.get_status()
        self.assertFalse(res1.get("connected", True))
        self.assertTrue(res1.get("degraded", False))
        self.assertTrue(service.is_circuit_open())

        # Call 2 to 100 during cooldown: must return degraded payload in <1ms without network calls
        start_time = time.perf_counter()
        for _ in range(50):
            res = service.get_status()
            self.assertTrue(res.get("degraded"))
            self.assertFalse(res.get("connected"))
        elapsed_ms = (time.perf_counter() - start_time) * 1000

        print(f"[PASS] QB circuit breaker cooldown verified: 50 requests took {elapsed_ms:.2f}ms (avg {elapsed_ms/50:.4f}ms per req)")
        self.assertLess(elapsed_ms, 50)  # Total 50 requests in <50ms (avg <1ms)

    def test_jellyfin_circuit_breaker_and_image_fast_fail(self):
        """Test JellyfinService circuit breaker and image proxy fast-fail when Jellyfin is unreachable."""
        mock_jf_client = MagicMock()
        mock_jf_client.host = "http://127.0.0.1:9998"
        mock_jf_client.api_key = "test_key"
        mock_jf_client.get_server_info.side_effect = requests.exceptions.ConnectTimeout("Timeout connect to 127.0.0.1:9998")

        jf_service = JellyfinService(
            db=MagicMock(),
            jellyfin_client=mock_jf_client,
            scraper=MagicMock(),
            logger=MagicMock(),
            deletion_tasks={},
            deletion_tasks_lock=MagicMock(),
            as_bool=lambda x, d: bool(x),
            parse_int_value=lambda x, d, minimum=0: int(x or d),
            safe_number=float,
            safe_exception_label=str,
            display_value_for_log=str,
            normalize_scraper_url=lambda x: str(x),
            normalize_external_subscription_url=lambda x: str(x),
            normalize_subscription_display_name=lambda a, b: str(b),
            follow_and_subscribe_actress=lambda: {},
            jellyfin_person_subscription_url_from_actress_id=lambda x: None,
            resolve_actress_subscription_url=lambda a, b, c: None,
            subscription_visible_for_actress=lambda *a: True,
            get_actress_subscription_url_candidates=lambda *a: [],
            get_subscription_delete_url_candidates=lambda x: [],
            subscription_url_exists=lambda x: False,
            ensure_followed_actress_subscriptions=lambda: {},
            normalize_internal_subscription_url=lambda x: None,
            normalize_item_id=lambda x: str(x),
            run_in_threadpool=lambda fn, *args: fn(*args),
        )

        # Trigger probe, connection fails and opens circuit breaker
        info = jf_service.probe_server_info()
        self.assertIsNone(info)
        self.assertTrue(jf_service.is_circuit_open())

        # Test disconnected_stats returns degraded payload
        stats = jf_service.disconnected_stats("offline")
        self.assertFalse(stats["connected"])
        self.assertTrue(stats["degraded"])

        # Subsequent probes take <1ms
        start_t = time.perf_counter()
        for _ in range(50):
            res = jf_service.probe_server_info()
            self.assertIsNone(res)
        elapsed = (time.perf_counter() - start_t) * 1000
        print(f"[PASS] Jellyfin circuit breaker verified: 50 probes took {elapsed:.2f}ms (avg {elapsed/50:.4f}ms per probe)")
        self.assertLess(elapsed, 50)

    def test_main_py_architecture_cleanup(self):
        """Test removal of qb_cache in main.py and deletion of dead router files."""
        self.assertFalse(hasattr(main, "qb_cache"), "qb_cache must be removed from main.py")
        self.assertFalse(hasattr(main, "qb_cache_lock"), "qb_cache_lock must be removed from main.py")

        dead_routers = [
            os.path.join(os.path.dirname(__file__), "routers", "qb_router.py"),
            os.path.join(os.path.dirname(__file__), "routers", "jellyfin_router.py"),
            os.path.join(os.path.dirname(__file__), "routers", "system_router.py"),
        ]
        for router_path in dead_routers:
            self.assertFalse(os.path.exists(router_path), f"Dead router file {router_path} should be deleted")

        print("[PASS] Architecture cleanup (qb_cache removal & router deletion) verified successfully")


if __name__ == "__main__":
    suite = unittest.TestLoader().loadTestsFromTestCase(TestResilienceAndArchitecture)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if not result.wasSuccessful():
        sys.exit(1)
