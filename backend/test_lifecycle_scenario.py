"""Scenario smoke tests for the personal media lifecycle.

This script uses a temporary SQLite database and mocked qB state. It should not
touch the real NAS, Jellyfin, qBittorrent, or project database.

Run from the repository root:
    python backend/test_lifecycle_scenario.py
"""

import asyncio
import os
import subprocess
import sys
import tempfile
import time
import warnings
from pathlib import Path

import media_policy

# The smoke test may run on a developer's global Python where unrelated tools
# install an incompatible chardet. Keep that host-level warning out of scenario
# output; production runtime should still expose dependency problems normally.
warnings.filterwarnings(
    "ignore",
    message=r"urllib3 .* doesn't match a supported version!",
)


def _prepare_temp_environment():
    temp_dir = tempfile.TemporaryDirectory(prefix="busterminal_lifecycle_")
    db_path = Path(temp_dir.name) / "movies.db"
    os.environ["DATA_DIR"] = temp_dir.name
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    os.environ.setdefault("LOG_LEVEL", "ERROR")
    return temp_dir


def _patch_external_services(main_module):
    class FakeSession:
        pass

    main_module._real_get_qb_session_for_tests = main_module.get_qb_session
    main_module._real_qb_get_server_state_for_tests = main_module.qb_api.get_server_state
    main_module.get_qb_session = lambda: (FakeSession(), "http://qb.local")
    main_module.qb_api.get_server_state = lambda session, base_url: {
        "free_space_on_disk": 10 * 1024 * 1024 * 1024 * 1024,
    }


def _stub_internal_session(module, get=None, post=None, delete=None):
    """把某个模块里 internal_session() 的出网请求替换为测试替身，返回还原函数。

    生产侧统一走 net_policy.internal_session()（内网直连），不再直接调用
    requests.get/delete，所以测试必须打在同一个接缝上，否则用例会真的发起
    网络请求。
    """
    handlers = {"get": get, "post": post, "delete": delete}

    def _dispatch(verb, url, kwargs):
        handler = handlers.get(verb)
        if handler is None:
            raise AssertionError(f"unexpected Jellyfin {verb} request: {url}")
        return handler(url, **kwargs)

    class FakeSession:
        def get(self, url, **kwargs):
            return _dispatch("get", url, kwargs)

        def post(self, url, **kwargs):
            return _dispatch("post", url, kwargs)

        def delete(self, url, **kwargs):
            return _dispatch("delete", url, kwargs)

    original_internal_session = module.internal_session
    module.internal_session = lambda: FakeSession()

    def _restore():
        module.internal_session = original_internal_session

    return _restore


def test_jellyfin_delete_guard(main_module):
    blocked = main_module.validate_jellyfin_delete_request({}, 1)
    assert blocked["success"] is False
    assert blocked["requires_confirmation"] is True

    main_module.db.set_config("jellyfin_delete_enabled", "true")
    missing_confirmation = main_module.validate_jellyfin_delete_request({"reason": "scenario"}, 1)
    assert missing_confirmation["success"] is False

    allowed = main_module.validate_jellyfin_delete_request(
        {
            "confirm": main_module.JELLYFIN_DELETE_CONFIRMATION,
            "reason": "scenario test",
        },
        1,
    )
    assert allowed["success"] is True


def test_jellyfin_delete_rejects_mixed_invalid_item_ids(main_module):
    from fastapi.testclient import TestClient

    client = TestClient(main_module.app)
    main_module.db.set_config("jellyfin_delete_enabled", "true")
    original_reload_config = main_module.jellyfin_client.reload_config
    original_delete_items = main_module.jellyfin_client.delete_items
    calls = []

    main_module.jellyfin_client.reload_config = lambda: calls.append("reload")
    main_module.jellyfin_client.delete_items = lambda item_ids: calls.append(item_ids) or {
        "success": True,
        "results": [],
    }

    payload = {
        "item_ids": ["valid-item-001", "../bad"],
        "confirm": main_module.JELLYFIN_DELETE_CONFIRMATION,
        "reason": "scenario",
    }
    try:
        sync_response = client.post("/api/jellyfin/items/delete", json=payload)
        async_response = client.post("/api/jellyfin/items/delete_async", json=payload)
    finally:
        main_module.jellyfin_client.reload_config = original_reload_config
        main_module.jellyfin_client.delete_items = original_delete_items

    assert sync_response.status_code == 200
    assert sync_response.json()["success"] is False
    assert sync_response.json()["message"] == "invalid_item_ids"
    assert async_response.status_code == 200
    assert async_response.json()["success"] is False
    assert async_response.json()["message"] == "invalid_item_ids"
    assert calls == []


def test_jellyfin_delete_guard_redacts_reason_in_logs(main_module):
    main_module.db.set_config("jellyfin_delete_enabled", "true")
    original_warning = main_module.logger.warning
    warning_calls = []

    def capture_warning(*args, **kwargs):
        warning_calls.append((args, kwargs))

    main_module.logger.warning = capture_warning
    try:
        reason = "cleanup C:/private/movie.mp4 api_key=secret-token"
        allowed = main_module.validate_jellyfin_delete_request(
            {
                "confirm": main_module.JELLYFIN_DELETE_CONFIRMATION,
                "reason": reason,
            },
            1,
        )
    finally:
        main_module.logger.warning = original_warning

    assert allowed["success"] is True
    assert allowed["reason"] == reason
    assert len(warning_calls) == 1
    args, _kwargs = warning_calls[0]
    assert args[0] == "Jellyfin delete authorized: item_count=%s reason=%s"
    assert args[1] == 1
    assert "C:/private" not in args[2]
    assert "secret-token" not in args[2]
    assert "<path redacted>" in args[2]
    assert "api_key=***" in args[2]


def test_scrape_monitor_url_logs_are_private_by_default(main_module):
    import scrape_monitor

    raw_url = "http://private-jellyfin.local:8096/emby?api_key=secret-token"
    original_flag = os.environ.get(scrape_monitor.ALLOW_UNSAFE_LOG_OUTPUT)
    os.environ.pop(scrape_monitor.ALLOW_UNSAFE_LOG_OUTPUT, None)
    try:
        masked = scrape_monitor.safe_url_for_log(raw_url)
        assert "private-jellyfin" not in masked
        assert "secret-token" not in masked
        assert masked.startswith("<redacted len=")

        os.environ[scrape_monitor.ALLOW_UNSAFE_LOG_OUTPUT] = "true"
        assert scrape_monitor.safe_url_for_log(raw_url) == raw_url
    finally:
        if original_flag is None:
            os.environ.pop(scrape_monitor.ALLOW_UNSAFE_LOG_OUTPUT, None)
        else:
            os.environ[scrape_monitor.ALLOW_UNSAFE_LOG_OUTPUT] = original_flag


def test_runtime_bool_config_accepts_common_truthy_values(main_module):
    from fastapi.testclient import TestClient

    original_values = {
        key: main_module.db.get_config(key)
        for key in (
            "jellyfin_delete_enabled",
            "jellyfin_auto_approve_candidates",
            "cleanup_enabled",
            "jellyfin_host",
            "jellyfin_api_key",
        )
    }
    try:
        main_module.db.set_config("jellyfin_delete_enabled", "1")
        allowed = main_module.validate_jellyfin_delete_request(
            {
                "confirm": main_module.JELLYFIN_DELETE_CONFIRMATION,
                "reason": "truthy config",
            },
            1,
        )
        assert allowed["success"] is True

        main_module.db.set_config("jellyfin_auto_approve_candidates", "yes")
        assert main_module.is_jellyfin_auto_approve_candidates_enabled() is True

        main_module.db.set_config("cleanup_enabled", "on")
        main_module.db.set_config("jellyfin_host", "")
        main_module.db.set_config("jellyfin_api_key", "")
        client = TestClient(main_module.app)
        stats_res = client.get("/api/jellyfin/stats")

        assert stats_res.status_code == 200
        stats_body = stats_res.json()
        assert stats_body["success"] is True
        assert stats_body["data"]["cleanup"]["enabled"] is True
    finally:
        bool_defaults = {
            "jellyfin_delete_enabled": "false",
            "jellyfin_auto_approve_candidates": "false",
            "cleanup_enabled": "false",
        }
        for key, value in original_values.items():
            if value is None:
                value = bool_defaults.get(key, "")
            main_module.db.set_config(key, value)


def test_jellyfin_item_id_normalization_rejects_path_injection(main_module):
    assert main_module.normalize_item_id("abcDEF_123-xyz") == "abcDEF_123-xyz"
    assert main_module.normalize_item_id("../Users") is None
    assert main_module.normalize_item_id("abc/def") is None
    assert main_module.normalize_item_id("abc?api_key=leak") is None

    item_ids = main_module.normalize_item_ids([
        "abcDEF_123-xyz",
        "abcDEF_123-xyz",
        "../Users",
        "abc/def",
        "abc?api_key=leak",
        "",
        "x" * 129,
    ])

    assert item_ids == ["abcDEF_123-xyz"]

    strict_ids, strict_error = main_module.validate_jellyfin_item_ids_payload([
        "abcDEF_123-xyz",
        "../Users",
    ])
    assert strict_ids == []
    assert strict_error == "invalid_item_ids"

    missing_ids, missing_error = main_module.validate_jellyfin_item_ids_payload([])
    assert missing_ids == []
    assert missing_error == "missing_or_invalid_item_ids"


def test_jellyfin_client_tolerates_dirty_library_payload(main_module):
    import jellyfin_api

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def fake_get(url, **kwargs):
        if url.endswith("/Views"):
            return FakeResponse({
                "Items": [
                    "bad-library",
                    {"Name": "Missing Id"},
                    {"Id": "lib-001"},
                    {"Id": "lib-002", "Name": "Movies", "CollectionType": "movies"},
                ]
            })
        if url.endswith("/Items"):
            return FakeResponse({
                "TotalRecordCount": "dirty-total",
                "Items": [
                    "bad-movie",
                    {"Name": "Missing Id"},
                    {
                        "Id": "movie-001",
                        "Name": "Dirty Favorite",
                        "ImageTags": None,
                        "UserData": {"Played": "false", "IsFavorite": "true", "PlayCount": "dirty"},
                        "MediaSources": [{"Size": "2048"}],
                        "Size": "1024",
                        "PremiereDate": 123,
                    },
                    {
                        "Id": "movie-002",
                        "Name": None,
                        "ImageTags": {"Primary": "tag-002"},
                        "UserData": "dirty-user-data",
                        "MediaSources": ["bad-source", {"Size": "dirty"}],
                        "Size": "dirty",
                        "PremiereDate": "2026-06-22T00:00:00.0000000Z",
                    },
                ],
            })
        raise AssertionError(f"unexpected Jellyfin URL: {url}")

    client = jellyfin_api.JellyfinClient()
    client.host = "http://jellyfin.local"
    client.api_key = "scenario-key"
    client.user_id = "user-001"
    restore_transport = _stub_internal_session(jellyfin_api, get=fake_get)
    try:
        libraries = client.get_libraries()
        movies = client.get_library_items(limit=20)
        not_favorite_movies = client.get_library_items(is_favorite=False, limit=20)
    finally:
        restore_transport()

    assert libraries == [
        {"id": "lib-001", "name": "lib-001", "type": "unknown"},
        {"id": "lib-002", "name": "Movies", "type": "movies"},
    ]
    assert movies["total"] == 4
    assert [item["jellyfin_id"] for item in movies["items"]] == ["movie-001", "movie-002"]
    assert movies["items"][0]["is_watched"] is False
    assert movies["items"][0]["is_favorite"] is True
    assert movies["items"][0]["play_count"] == 0
    assert movies["items"][0]["file_size"] == 2048
    assert movies["items"][1]["title"] == "movie-002"
    assert movies["items"][1]["release_date"] == "2026-06-22"
    assert [item["jellyfin_id"] for item in not_favorite_movies["items"]] == ["movie-002"]


def test_jellyfin_server_probe_uses_short_timeout(main_module):
    import jellyfin_api

    class FakeResponse:
        status_code = 503

    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs.get("timeout")))
        return FakeResponse()

    client = jellyfin_api.JellyfinClient()
    client.host = "http://jellyfin.local"
    client.api_key = "probe-key"
    restore_transport = _stub_internal_session(jellyfin_api, get=fake_get)
    try:
        result = client.get_server_info()
    finally:
        restore_transport()

    assert result is None
    assert calls == [
        ("http://jellyfin.local/System/Info", jellyfin_api.JELLYFIN_PROBE_TIMEOUT_SECONDS)
    ]
    assert jellyfin_api.JELLYFIN_PROBE_TIMEOUT_SECONDS == 3


def test_jellyfin_client_returns_only_dict_items_from_collection_endpoints(main_module):
    import jellyfin_api

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def fake_get(url, **kwargs):
        params = kwargs.get("params") or {}
        if url.endswith("/Persons"):
            return FakeResponse({"Items": ["bad-person", {"Name": "Clean Person"}]})
        if "/Users/" in url and url.endswith("/Items"):
            include_type = params.get("IncludeItemTypes")
            filters = params.get("Filters")
            if filters == "IsFavorite" and include_type == "Movie":
                return FakeResponse({"Items": ["bad-movie", {"Name": "Favorite Movie"}]})
            if filters == "IsFavorite" and include_type == "Person":
                return FakeResponse({"Items": "dirty-people"})
            if filters == "IsPlayed" and include_type == "Movie":
                return FakeResponse({"Items": [None, {"Name": "Watched Movie"}]})
        if url.endswith("/Items"):
            return FakeResponse({"Items": ["bad-result", {"Name": "Found Movie"}]})
        raise AssertionError(f"unexpected Jellyfin URL: {url}")

    client = jellyfin_api.JellyfinClient()
    client.host = "http://jellyfin.local"
    client.api_key = "scenario-key"
    client.user_id = "user-001"
    restore_transport = _stub_internal_session(jellyfin_api, get=fake_get)
    try:
        people = client.search_people("Clean")
        favorite_movies = client.get_favorite_movies()
        favorite_people = client.get_favorite_people()
        watched_movies = client.get_watched_movies()
        found_movie = client.get_item_by_name("Found")
    finally:
        restore_transport()

    assert people == [{"Name": "Clean Person"}]
    assert favorite_movies == [{"Name": "Favorite Movie"}]
    assert favorite_people == []
    assert watched_movies == [{"Name": "Watched Movie"}]
    assert found_movie == {"Name": "Found Movie"}


def test_jellyfin_client_favorite_people_skips_fallback_when_primary_has_data(main_module):
    import jellyfin_api

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if "/Users/user-001/Items" in url:
            return FakeResponse({"Items": [
                {"Id": "person-primary-001", "Name": "Primary Favorite Person"}
            ]})
        if url.endswith("/Persons"):
            raise AssertionError("Persons fallback should not run when primary favorite people exist")
        raise AssertionError(f"unexpected Jellyfin URL: {url}")

    client = jellyfin_api.JellyfinClient()
    client.host = "http://jellyfin.local"
    client.api_key = "scenario-key"
    client.user_id = "user-001"
    restore_transport = _stub_internal_session(jellyfin_api, get=fake_get)
    try:
        favorite_people = client.get_favorite_people()
    finally:
        restore_transport()

    assert favorite_people == [
        {"Id": "person-primary-001", "Name": "Primary Favorite Person"}
    ]
    assert calls == ["http://jellyfin.local/Users/user-001/Items"]


def test_jellyfin_client_favorite_people_falls_back_to_persons(main_module):
    import jellyfin_api

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def fake_get(url, **kwargs):
        params = kwargs.get("params") or {}
        if "/Users/user-001/Items" in url:
            assert params.get("IncludeItemTypes") == "Person"
            return FakeResponse({"Items": []})
        if url.endswith("/Persons"):
            assert params.get("UserId") == "user-001"
            assert params.get("IsFavorite") == "true"
            return FakeResponse({
                "Items": [
                    {"Id": "person-fav-001", "Name": "Favorite Person", "UserData": {"IsFavorite": True}},
                    {"Id": "person-not-fav-001", "Name": "Ignored Person", "UserData": {"IsFavorite": False}},
                    {"Id": "person-dirty-001", "Name": "Dirty Person"},
                    "not-a-person",
                ]
            })
        raise AssertionError(f"unexpected Jellyfin URL: {url}")

    client = jellyfin_api.JellyfinClient()
    client.host = "http://jellyfin.local"
    client.api_key = "scenario-key"
    client.user_id = "user-001"
    restore_transport = _stub_internal_session(jellyfin_api, get=fake_get)
    try:
        favorite_people = client.get_favorite_people()
    finally:
        restore_transport()

    assert favorite_people == [
        {"Id": "person-fav-001", "Name": "Favorite Person", "UserData": {"IsFavorite": True}}
    ]


def test_delete_task_id_normalization_rejects_invalid_values(main_module):
    valid = "6f4d6d2e-d9a1-4c64-889d-111111111111"
    assert main_module.normalize_task_id(valid) == valid
    assert main_module.normalize_task_id("../delete") is None
    assert main_module.normalize_task_id("not-a-uuid") is None
    assert main_module.normalize_task_id("x" * 200) is None


async def test_delete_task_endpoints_reject_invalid_task_id(main_module):
    try:
        await main_module.get_delete_progress("../delete")
    except main_module.HTTPException as exc:
        assert exc.status_code == 400
        assert exc.detail == "invalid_task_id"
    else:
        raise AssertionError("invalid delete progress task id should be rejected")

    try:
        await main_module.cancel_delete_task("not-a-uuid")
    except main_module.HTTPException as exc:
        assert exc.status_code == 400
        assert exc.detail == "invalid_task_id"
    else:
        raise AssertionError("invalid cancel task id should be rejected")


async def test_jellyfin_image_rejects_invalid_item_id_before_remote_request(main_module):
    import requests

    original_get = requests.get

    def fail_if_called(*args, **kwargs):
        raise AssertionError("invalid Jellyfin image id should be rejected before remote request")

    requests.get = fail_if_called
    try:
        try:
            await main_module.proxy_jellyfin_image("../Users")
        except main_module.HTTPException as exc:
            assert exc.status_code == 400
            assert exc.detail == "invalid_jellyfin_item_id"
        else:
            raise AssertionError("invalid Jellyfin image id should raise HTTPException")
    finally:
        requests.get = original_get


def test_jellyfin_client_rejects_invalid_item_ids_before_remote_request(main_module):
    import requests

    client = main_module.jellyfin_client
    original_host = client.host
    original_api_key = client.api_key
    original_user_id = client.user_id
    original_discover_user_id = client._discover_user_id
    original_delete = requests.delete
    original_request = requests.request

    def fail_if_called(*args, **kwargs):
        raise AssertionError("invalid Jellyfin item id should be rejected before remote request")

    client.host = "http://jellyfin.local"
    client.api_key = "scenario-key"
    client.user_id = "user-001"
    client._discover_user_id = lambda: "user-001"
    requests.delete = fail_if_called
    requests.request = fail_if_called
    try:
        assert client.delete_item("../Users") is False
        assert client.toggle_favorite_item("abc/def", True) is False
        result = client.delete_items(["../Users"])
    finally:
        client.host = original_host
        client.api_key = original_api_key
        client.user_id = original_user_id
        client._discover_user_id = original_discover_user_id
        requests.delete = original_delete
        requests.request = original_request

    assert result["results"][0]["error"] == "invalid_item_id"


def test_jellyfin_bulk_delete_errors_are_sanitized(main_module):
    import requests
    import jellyfin_api

    client = main_module.jellyfin_client
    original_host = client.host
    original_api_key = client.api_key

    def fail_with_sensitive_message(*args, **kwargs):
        raise requests.RequestException(
            "failed http://private-jellyfin.local/Items/item-001?api_key=secret-token"
        )

    client.host = "http://private-jellyfin.local"
    client.api_key = "secret-token"
    restore_transport = _stub_internal_session(jellyfin_api, delete=fail_with_sensitive_message)
    try:
        result = client.delete_items(["item-001"])
    finally:
        restore_transport()
        client.host = original_host
        client.api_key = original_api_key

    assert result["success"] is True
    item_result = result["results"][0]
    assert item_result["success"] is False
    assert item_result["error"] == "RequestException"
    assert "private-jellyfin" not in str(item_result)
    assert "secret-token" not in str(item_result)


def test_legacy_jellyfin_cleanup_delete_guard(main_module):
    import scrape_monitor

    calls = []
    original_delete = scrape_monitor.requests.delete
    original_delete_enabled = main_module.db.get_config("jellyfin_delete_enabled")
    main_module.db.set_config("jellyfin_delete_enabled", "false")

    def fail_if_called(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("legacy cleanup delete should be blocked before calling Jellyfin")

    scrape_monitor.requests.delete = fail_if_called
    try:
        result = main_module.jellyfin_cleanup._delete_movie({
            "id": "jf-danger-001",
            "name": "Dangerous Delete",
            "path": "",
        })
        main_module.db.set_config("jellyfin_delete_enabled", "true")
        enabled_result = main_module.jellyfin_cleanup._delete_movie({
            "id": "jf-danger-002",
            "name": "Still Must Not Delete",
            "path": "",
        })
    finally:
        scrape_monitor.requests.delete = original_delete
        main_module.db.set_config("jellyfin_delete_enabled", original_delete_enabled or "false")

    assert result is False
    assert enabled_result is False
    assert calls == []


def test_legacy_jellyfin_cleanup_rejects_invalid_item_id(main_module):
    import scrape_monitor

    calls = []
    original_delete = scrape_monitor.requests.delete
    main_module.db.set_config("jellyfin_delete_enabled", "true")
    main_module.jellyfin_cleanup.jellyfin_url = "http://jellyfin.local"
    main_module.jellyfin_cleanup.jellyfin_api_key = "scenario-key"

    def fail_if_called(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("legacy cleanup should reject invalid item id before calling Jellyfin")

    scrape_monitor.requests.delete = fail_if_called
    try:
        result = main_module.jellyfin_cleanup._delete_movie({
            "id": "../Users",
            "name": "Invalid Item",
            "path": "",
        })
    finally:
        scrape_monitor.requests.delete = original_delete

    assert result is False
    assert calls == []


def test_legacy_jellyfin_cleanup_tolerates_dirty_config(main_module):
    main_module.jellyfin_cleanup.progress_threshold = 80
    main_module.jellyfin_cleanup.run_hour = 3
    main_module.db.set_config("cleanup_enabled", "false")
    main_module.db.set_config("cleanup_progress_threshold", "not-a-progress")
    main_module.db.set_config("cleanup_run_hour", "99")

    main_module.jellyfin_cleanup.reload_config()

    assert main_module.jellyfin_cleanup.cleanup_enabled is False
    assert main_module.jellyfin_cleanup.progress_threshold == 80
    assert main_module.jellyfin_cleanup.run_hour == 3

    main_module.db.set_config("cleanup_progress_threshold", 80)
    main_module.db.set_config("cleanup_run_hour", 3)


def test_empty_directory_cleanup_respects_managed_roots(main_module, temp_dir):
    import scrape_monitor
    from lifecycle_manager import LifecycleManager

    original_roots = os.environ.get("BUS_TERMINAL_MANAGED_MEDIA_ROOTS")
    temp_root = Path(temp_dir.name)
    managed_root = temp_root / "downloads"
    os.environ["BUS_TERMINAL_MANAGED_MEDIA_ROOTS"] = str(managed_root)

    unmanaged_dir = temp_root / "outside-empty"
    unmanaged_dir.mkdir()
    managed_dir = managed_root / "empty-parent"
    try:
        assert scrape_monitor.safe_remove_empty_managed_dir(str(unmanaged_dir), "test") is False
        assert unmanaged_dir.exists()

        managed_dir.mkdir(parents=True)
        assert LifecycleManager._safe_remove_empty_managed_dir(str(managed_dir), "test") is True
        assert not managed_dir.exists()
    finally:
        if original_roots is None:
            os.environ.pop("BUS_TERMINAL_MANAGED_MEDIA_ROOTS", None)
        else:
            os.environ["BUS_TERMINAL_MANAGED_MEDIA_ROOTS"] = original_roots


def test_cleanup_candidate_is_non_destructive(main_module):
    candidate_id = main_module.db.upsert_cleanup_candidate(
        jellyfin_id="jf-001",
        movie_id="MOV-CLEAN-001",
        title="Cleanup Candidate",
        file_path="/media/Cleanup Candidate.mp4",
        progress=100,
        is_favorite=False,
        reason="watched_not_favorite",
    )
    candidates = main_module.db.get_cleanup_candidates()
    assert any(row["id"] == candidate_id for row in candidates)

    events = main_module.db.get_movie_lifecycle_events("MOV-CLEAN-001")
    assert events[0]["event_type"] == "cleanup_candidate_detected"
    assert events[0]["cleanup_status"] == "candidate"


def test_cleanup_auto_approval_never_deletes(main_module):
    cleanup = main_module.jellyfin_cleanup
    original_get_movies = cleanup._get_library_movies
    original_get_progress = cleanup._get_playback_progress
    original_is_favorite = cleanup._is_favorite
    original_url = cleanup.jellyfin_url
    original_key = cleanup.jellyfin_api_key
    original_threshold = cleanup.progress_threshold
    cleanup.jellyfin_url = "http://jellyfin.invalid"
    cleanup.jellyfin_api_key = "test-only"
    cleanup.progress_threshold = 80
    cleanup._get_library_movies = lambda: [
        {"Id": "jf-auto-cleanup-001", "Name": "AUTO-CLN-001 Candidate", "Path": "C:/private/movie.mp4"}
    ]
    cleanup._get_playback_progress = lambda _item_id: 95
    cleanup._is_favorite = lambda _item_id: False
    main_module.db.set_config("automation_mode", "auto_approve_low_risk")
    main_module.db.set_config("auto_cleanup_enabled", "true")
    try:
        result = cleanup._run_cleanup()
    finally:
        cleanup._get_library_movies = original_get_movies
        cleanup._get_playback_progress = original_get_progress
        cleanup._is_favorite = original_is_favorite
        cleanup.jellyfin_url = original_url
        cleanup.jellyfin_api_key = original_key
        cleanup.progress_threshold = original_threshold
        main_module.db.set_config("automation_mode", "suggest")
        main_module.db.set_config("auto_cleanup_enabled", "false")

    assert result["candidate_count"] == 1
    assert result["auto_approved_count"] == 1
    assert "path" not in result["candidates"][0]
    assert "jellyfin_id" not in result["candidates"][0]
    approved = main_module.db.get_cleanup_candidates("approved")
    assert any(row["jellyfin_id"] == "jf-auto-cleanup-001" for row in approved)
    assert result["candidates"][0]["status"] == "approved"


def test_registered_cleanup_loop_uses_shared_stop_signal(main_module):
    class FakeStopEvent:
        def __init__(self):
            self.stopped = False
            self.waits = []

        def is_set(self):
            return self.stopped

        def wait(self, timeout):
            self.waits.append(timeout)
            self.stopped = True
            return True

    class FakeRegistry:
        def __init__(self):
            self.successes = []
            self.errors = []

        def mark_success(self, name, summary=None, **_kwargs):
            self.successes.append((name, summary))

        def mark_error(self, name, exc, **_kwargs):
            self.errors.append((name, type(exc).__name__))

    cleanup = main_module.jellyfin_cleanup
    original_reload = cleanup.reload_config
    original_enabled = cleanup.cleanup_enabled
    stop_event = FakeStopEvent()
    registry = FakeRegistry()
    cleanup.reload_config = lambda: setattr(cleanup, "cleanup_enabled", False)
    try:
        cleanup.run_registered_loop(stop_event, registry)
    finally:
        cleanup.reload_config = original_reload
        cleanup.cleanup_enabled = original_enabled

    assert stop_event.waits == [3600]
    assert registry.errors == []
    assert registry.successes == [
        (
            "jellyfin_cleanup_candidate_scan",
            {"status": "idle", "enabled": False, "run_hour": cleanup.run_hour},
        )
    ]
    assert cleanup.is_running is False


def test_cleanup_candidate_decisions_are_audited_without_deletion(main_module):
    from fastapi.testclient import TestClient

    candidate_id = main_module.db.upsert_cleanup_candidate(
        jellyfin_id="jf-review-001",
        movie_id="MOV-CLEAN-REVIEW-001",
        title="Cleanup Review",
        file_path="C:/private/library/Cleanup Review.mp4",
        progress=98,
        is_favorite=False,
        reason="watched_not_favorite",
    )
    original_delete_item = main_module.jellyfin_client.delete_item
    original_delete_items = main_module.jellyfin_client.delete_items
    main_module.jellyfin_client.delete_item = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("cleanup approval must not delete Jellyfin items")
    )
    main_module.jellyfin_client.delete_items = main_module.jellyfin_client.delete_item
    client = TestClient(main_module.app)
    try:
        listed = client.get("/api/cleanup/candidates", params={"status": "candidate"})
        approved = client.post(
            f"/api/cleanup/candidates/{candidate_id}/approve",
            json={"actor": "reviewer", "reason": "reviewed for later cleanup"},
        )
        duplicate = client.post(
            f"/api/cleanup/candidates/{candidate_id}/approve",
            json={"actor": "reviewer", "reason": "duplicate approval"},
        )
        conflict = client.post(
            f"/api/cleanup/candidates/{candidate_id}/reject",
            json={"actor": "reviewer", "reason": "conflicting rejection"},
        )
    finally:
        main_module.jellyfin_client.delete_item = original_delete_item
        main_module.jellyfin_client.delete_items = original_delete_items

    assert listed.status_code == 200
    listed_text = listed.text
    assert candidate_id in listed_text
    assert "file_path" not in listed_text
    assert "C:/private" not in listed_text
    assert approved.status_code == 200
    assert approved.json()["success"] is True
    assert approved.json()["delete_performed"] is False
    assert approved.json()["data"]["status"] == "approved"
    assert approved.json()["data"]["decision_actor"] == "reviewer"
    assert duplicate.json()["success"] is True
    assert duplicate.json()["duplicate"] is True
    assert conflict.json()["success"] is False
    assert conflict.json()["message"] == "cleanup_candidate_state_conflict"

    main_module.db.upsert_cleanup_candidate(
        jellyfin_id="jf-review-001",
        movie_id="MOV-CLEAN-REVIEW-001",
        title="Cleanup Review",
        reason="repeat_scan",
    )
    approved_rows = main_module.db.get_cleanup_candidates("approved")
    assert any(row["id"] == candidate_id for row in approved_rows)

    lifecycle = main_module.db.get_movie_lifecycle(movie_id="MOV-CLEAN-REVIEW-001")[0]
    events = main_module.db.get_movie_lifecycle_events("MOV-CLEAN-REVIEW-001", 20)
    assert lifecycle["cleanup_status"] == "approved"
    assert any(event["event_type"] == "cleanup_candidate_approved" for event in events)

    invalid = client.get("/api/cleanup/candidates", params={"status": "executing"})
    assert invalid.status_code == 422


def test_jellyfin_delete_rejects_batches_over_hard_limit(main_module):
    from fastapi.testclient import TestClient

    main_module.db.set_config("jellyfin_delete_enabled", "true")
    calls = []
    original_delete_items = main_module.jellyfin_client.delete_items
    main_module.jellyfin_client.delete_items = lambda item_ids: calls.append(item_ids) or {"success": True}
    payload = {
        "item_ids": [f"item-{index:03d}" for index in range(main_module.JELLYFIN_DELETE_MAX_ITEMS + 1)],
        "confirm": main_module.JELLYFIN_DELETE_CONFIRMATION,
        "reason": "approved cleanup batch",
    }
    client = TestClient(main_module.app)
    try:
        sync_response = client.post("/api/jellyfin/items/delete", json=payload)
        async_response = client.post("/api/jellyfin/items/delete_async", json=payload)
    finally:
        main_module.jellyfin_client.delete_items = original_delete_items

    expected = f"delete_item_limit_exceeded:{main_module.JELLYFIN_DELETE_MAX_ITEMS}"
    assert sync_response.json()["success"] is False
    assert sync_response.json()["message"] == expected
    assert async_response.json()["success"] is False
    assert async_response.json()["message"] == expected
    assert calls == []


def test_download_permission_guardrails(main_module):
    original_get_qb_session = main_module.get_qb_session
    session_calls = []

    def fake_get_qb_session():
        session_calls.append(True)
        return original_get_qb_session()

    main_module.get_qb_session = fake_get_qb_session
    main_module.db.set_config("automation_mode", "auto_approve_low_risk")
    main_module.db.set_config("auto_download_enabled", "false")
    main_module.db.set_config("auto_download_daily_limit", "3")
    main_module.db.set_config("disk_threshold_gb", "1")

    try:
        denied = main_module.evaluate_download_permission(
            "MOV-DL-001",
            "magnet:?xt=urn:btih:scenario001",
            "Auto Disabled",
            "subscription",
            automated=True,
        )
        assert denied["allowed"] is False
        assert "auto_download_disabled" in denied["reasons"]
        assert session_calls == []
        denied_events = main_module.db.get_movie_lifecycle_events("MOV-DL-001")
        assert denied_events[0]["event_type"] == "download_permission_denied"
        assert denied_events[0]["download_status"] == "blocked"

        main_module.db.set_config("auto_download_enabled", "true")
        allowed = main_module.evaluate_download_permission(
            "MOV-DL-002",
            "magnet:?xt=urn:btih:scenario002",
            "Allowed Download",
            "subscription",
            automated=True,
        )
        assert allowed["allowed"] is True
        assert len(session_calls) == 1

        task_id = main_module.db.add_to_queue(
            magnet="magnet:?xt=urn:btih:scenario002",
            movie_id="MOV-DL-002",
            title="Allowed Download",
            source_url="subscription",
        )
        assert task_id

        duplicate = main_module.evaluate_download_permission(
            "MOV-DL-002",
            "magnet:?xt=urn:btih:scenario002",
            "Allowed Download",
            "subscription",
            automated=True,
        )
        assert duplicate["allowed"] is False
        assert "active_queue_exists" in duplicate["reasons"]
        assert "duplicate_queue_or_magnet" in duplicate["reasons"]
        assert len(session_calls) == 1

        main_module.db.set_config("auto_download_daily_limit", "1")
        daily_limited = main_module.evaluate_download_permission(
            "MOV-DL-003",
            "magnet:?xt=urn:btih:scenario003",
            "Daily Limited",
            "subscription",
            automated=True,
        )
        assert daily_limited["allowed"] is False
        assert "daily_limit_reached" in daily_limited["reasons"]
        assert len(session_calls) == 1

        main_module.db.set_config("auto_download_daily_limit", "not-a-limit")
        main_module.db.set_config("disk_threshold_gb", "not-a-threshold")
        dirty_config_allowed = main_module.evaluate_download_permission(
            "MOV-DL-004",
            "magnet:?xt=urn:btih:scenario004",
            "Dirty Config Download",
            "subscription",
            automated=True,
        )
        assert dirty_config_allowed["allowed"] is True
        assert "daily_limit_reached" not in dirty_config_allowed["reasons"]
        assert "disk_below_threshold" not in dirty_config_allowed["reasons"]
        assert len(session_calls) == 2
    finally:
        main_module.get_qb_session = original_get_qb_session


def _seed_queue_entry(main_module, movie_id, magnet, title, save_path=None):
    """Insert a fresh pending download_queue entry and return its task id."""
    return main_module.db.add_to_queue(
        magnet=magnet,
        movie_id=movie_id,
        title=title,
        source_url="subscription",
        save_path=save_path,
    )


def test_download_queue_list_pagination_and_filter(main_module):
    db = main_module.db
    ids = [
        _seed_queue_entry(main_module, "MOV-Q-LIST-1", "magnet:?xt=urn:btih:qlist001", "Queue List 1"),
        _seed_queue_entry(main_module, "MOV-Q-LIST-2", "magnet:?xt=urn:btih:qlist002", "Queue List 2"),
        _seed_queue_entry(main_module, "MOV-Q-LIST-3", "magnet:?xt=urn:btih:qlist003", "Queue List 3"),
    ]
    # Push two of them into distinct terminal-ish states so the filter is meaningful.
    db.update_download_status(ids[1], "submitted")
    db.update_download_status(ids[2], "failed", "seed failure")

    # Status filter returns only matching rows.
    failed_page = db.list_download_queue(status="failed", limit=200, offset=0)
    assert all(item["status"] == "failed" for item in failed_page["items"])
    assert ids[2] in {item["id"] for item in failed_page["items"]}
    assert failed_page["total"] == len(failed_page["items"])

    # by_status aggregate is present and reflects our seeded rows.
    assert failed_page["by_status"]["failed"] >= 1
    assert failed_page["by_status"]["submitted"] >= 1
    assert failed_page["by_status"]["pending"] >= 1

    # Pagination: limit is respected and offset advances the window.
    first = db.list_download_queue(limit=1, offset=0)
    assert len(first["items"]) == 1
    assert first["total"] >= 3
    second = db.list_download_queue(limit=1, offset=1)
    assert len(second["items"]) == 1
    assert first["items"][0]["id"] != second["items"][0]["id"]


def test_download_queue_retry_sets_next_retry_at(main_module):
    db = main_module.db

    # waiting_external_service -> retry sets next_retry_at without changing status.
    waiting_id = _seed_queue_entry(
        main_module, "MOV-Q-RETRY-1", "magnet:?xt=urn:btih:qretry001", "Queue Retry Waiting"
    )
    db.update_download_status(waiting_id, "waiting_external_service")
    before = db.get_download_queue_entry(waiting_id)
    assert before["next_retry_at"] is None

    result = db.retry_download_now(waiting_id)
    assert result["ok"] is True
    assert result["status"] == "waiting_external_service"
    after = db.get_download_queue_entry(waiting_id)
    assert after["next_retry_at"] is not None
    assert after["status"] == "waiting_external_service"

    # failed & retry_count < 5 is retryable.
    fail_id = _seed_queue_entry(
        main_module, "MOV-Q-RETRY-2", "magnet:?xt=urn:btih:qretry002", "Queue Retry Failed"
    )
    db.update_download_status(fail_id, "failed", "boom")  # retry_count -> 1
    assert db.retry_download_now(fail_id)["ok"] is True

    # failed & retry_count >= 5 (dead task) is NOT retryable via retry.
    dead_id = _seed_queue_entry(
        main_module, "MOV-Q-RETRY-3", "magnet:?xt=urn:btih:qretry003", "Queue Retry Dead"
    )
    for _ in range(5):
        db.update_download_status(dead_id, "failed", "boom")
    dead_entry = db.get_download_queue_entry(dead_id)
    assert int(dead_entry["retry_count"]) >= 5
    dead_result = db.retry_download_now(dead_id)
    assert dead_result["ok"] is False
    assert dead_result["reason"] == "invalid_state_for_retry"

    # Unknown task id is rejected cleanly.
    missing = db.retry_download_now("does-not-exist")
    assert missing["ok"] is False
    assert missing["reason"] == "task_not_found"


def test_download_queue_recover_failed_to_pending(main_module):
    db = main_module.db

    # A dead failed task (retry_count >= 5) recovers back to pending, counter reset.
    dead_id = _seed_queue_entry(
        main_module, "MOV-Q-REC-1", "magnet:?xt=urn:btih:qrec001", "Queue Recover Dead"
    )
    for _ in range(5):
        db.update_download_status(dead_id, "failed", "boom")
    recovered = db.recover_download(dead_id)
    assert recovered["ok"] is True
    assert recovered["status"] == "pending"
    entry = db.get_download_queue_entry(dead_id)
    assert entry["status"] == "pending"
    assert int(entry["retry_count"]) == 0
    assert entry["next_retry_at"] is None
    assert entry["error_message"] is None

    # Conflict: a recoverable task cannot recover if an active entry already
    # exists for the same movie/magnet (partial unique index allows one active).
    # Use a cancelled task so it is genuinely inactive and re-enqueue creates a
    # new active row (a failed row with retry_count < 5 still counts as active).
    conflict_id = _seed_queue_entry(
        main_module, "MOV-Q-REC-2", "magnet:?xt=urn:btih:qrec002", "Queue Recover Conflict"
    )
    assert db.cancel_download(conflict_id)["ok"] is True
    # Re-adding for the same movie/magnet now creates a NEW active pending row.
    active_id = db.add_to_queue(
        magnet="magnet:?xt=urn:btih:qrec002",
        movie_id="MOV-Q-REC-2",
        title="Queue Recover Conflict",
        source_url="subscription",
    )
    assert active_id != conflict_id
    conflict = db.recover_download(conflict_id)
    assert conflict["ok"] is False
    assert conflict["reason"] == "recover_conflict_active_exists"


def test_download_queue_cancel_frees_dedup_and_refuses_submitted(main_module):
    db = main_module.db

    # Cancelling a pending task frees the dedup slot so the movie can re-enqueue.
    cancel_id = _seed_queue_entry(
        main_module, "MOV-Q-CANCEL-1", "magnet:?xt=urn:btih:qcancel001", "Queue Cancel Pending"
    )
    assert db.get_active_queue_entry(movie_id="MOV-Q-CANCEL-1") is not None
    cancelled = db.cancel_download(cancel_id)
    assert cancelled["ok"] is True
    assert cancelled["status"] == "cancelled"
    assert db.get_active_queue_entry(movie_id="MOV-Q-CANCEL-1") is None
    reenqueued = db.add_to_queue(
        magnet="magnet:?xt=urn:btih:qcancel001",
        movie_id="MOV-Q-CANCEL-1",
        title="Queue Cancel Pending",
        source_url="subscription",
    )
    assert reenqueued != cancel_id

    # Submitted tasks (already handed to qB) refuse cancellation.
    submitted_id = _seed_queue_entry(
        main_module, "MOV-Q-CANCEL-2", "magnet:?xt=urn:btih:qcancel002", "Queue Cancel Submitted"
    )
    db.update_download_status(submitted_id, "submitted")
    refused = db.cancel_download(submitted_id)
    assert refused["ok"] is False
    assert refused["reason"] == "cannot_cancel_in_state"


def test_download_queue_routes_via_testclient(main_module):
    from fastapi.testclient import TestClient

    client = TestClient(main_module.app)
    db = main_module.db

    # Seed a waiting task we can retry through the HTTP layer.
    waiting_id = _seed_queue_entry(
        main_module, "MOV-Q-API-1", "magnet:?xt=urn:btih:qapi001", "Queue API Waiting"
    )
    db.update_download_status(waiting_id, "waiting_external_service")

    # List endpoint returns the safe serialized payload (no raw magnet/save_path).
    list_resp = client.get("/api/download/queue", params={"status": "waiting_external_service", "limit": 50})
    assert list_resp.status_code == 200
    body = list_resp.json()
    assert body["success"] is True
    assert "items" in body["data"]
    assert "by_status" in body["data"]
    sample = next((i for i in body["data"]["items"] if i["id"] == waiting_id), None)
    assert sample is not None
    assert "magnet" not in sample
    assert "save_path" not in sample
    assert "save_path_display" in sample

    # Retry endpoint succeeds and schedules the task.
    retry_resp = client.post(f"/api/download/queue/{waiting_id}/retry")
    assert retry_resp.status_code == 200
    assert retry_resp.json()["success"] is True
    assert db.get_download_queue_entry(waiting_id)["next_retry_at"] is not None

    # Unknown id yields a clean failure envelope, not a 500.
    missing_resp = client.post("/api/download/queue/no-such-task/retry")
    assert missing_resp.status_code == 200
    assert missing_resp.json()["success"] is False
    assert missing_resp.json()["message"] == "task_not_found"

    # Invalid status filter is rejected by the service layer.
    bad_filter = client.get("/api/download/queue", params={"status": "not-a-status"})
    assert bad_filter.status_code == 200
    assert bad_filter.json()["success"] is False
    assert bad_filter.json()["message"] == "invalid_status_filter"


async def test_automation_candidate_approval_and_rejection(main_module):
    queue_count_before = main_module.db.count_download_queue_since(0, automated_only=False)
    approved = main_module.db.upsert_automation_candidate(
        candidate_type="favorite_actor",
        subject_id="actor-001",
        subject_name="Scenario Actor",
        source_movie_id="MOV-FAV-001",
        source_title="Favorite Movie",
        source="jellyfin_favorite",
        recommended_action="follow_actor",
        confidence=0.9,
        reason="favorite_movie_actor",
        payload={"actor_page_url": "https://www.javbus.com/star/actor-001"},
    )

    approval_res = await main_module.approve_automation_candidate(
        approved["id"],
        main_module.AutomationCandidateDecisionRequest(actor="scenario", reason="user approved"),
    )
    assert approval_res["success"] is True
    assert approval_res["data"]["status"] == "approved"
    assert approval_res["application"]["subscription_visible"] is True

    actress = main_module.db.get_actress("actor-001")
    assert actress is not None
    assert bool(actress["is_followed"]) is True
    subscriptions = main_module.db.get_subscriptions()
    assert any(row["url"] == "https://www.javbus.com/star/actor-001" for row in subscriptions)
    assert main_module.db.count_download_queue_since(0, automated_only=False) == queue_count_before

    rejected = main_module.db.upsert_automation_candidate(
        candidate_type="favorite_actor",
        subject_id="actor-002",
        subject_name="Rejected Actor",
        source_movie_id="MOV-FAV-002",
        source_title="Rejected Favorite",
        source="jellyfin_favorite",
        recommended_action="follow_actor",
        confidence=0.7,
        reason="favorite_movie_actor",
        payload={"actor_page_url": "https://example.test/actor-002"},
    )
    rejection_res = await main_module.reject_automation_candidate(
        rejected["id"],
        main_module.AutomationCandidateDecisionRequest(actor="scenario", reason="not relevant"),
    )
    assert rejection_res["success"] is True
    assert rejection_res["data"]["status"] == "rejected"
    assert main_module.db.get_actress("actor-002") is None


async def test_automation_candidate_bulk_approval(main_module):
    first = main_module.db.upsert_automation_candidate(
        candidate_type="favorite_actor",
        subject_id="actor-bulk-001",
        subject_name="Bulk Actor One",
        source_movie_id="MOV-BULK-001",
        source_title="Bulk Favorite One",
        source="jellyfin_favorite",
        recommended_action="review_follow_and_subscribe",
        confidence=0.9,
        reason="favorite_movie_actor",
        payload={"actor_page_url": "https://www.javbus.com/star/actor-bulk-001"},
    )
    second = main_module.db.upsert_automation_candidate(
        candidate_type="favorite_actor",
        subject_id="actor-bulk-002",
        subject_name="Bulk Actor Two",
        source_movie_id="MOV-BULK-002",
        source_title="Bulk Favorite Two",
        source="jellyfin_favorite",
        recommended_action="review_follow_and_subscribe",
        confidence=0.8,
        reason="favorite_movie_actor",
        payload={"actor_page_url": "https://www.javbus.com/star/actor-bulk-002"},
    )

    res = await main_module.approve_all_automation_candidates(
        main_module.AutomationCandidateDecisionRequest(actor="scenario", reason="bulk approve")
    )

    assert res["success"] is True
    assert res["report"]["approved"] >= 2
    assert "https://www.javbus.com/star/actor-bulk-001" in res["report"]["subscription_urls"]
    assert "https://www.javbus.com/star/actor-bulk-002" in res["report"]["subscription_urls"]
    assert any(
        "https://www.javbus.com/star/actor-bulk-001" in group
        for group in res["report"]["subscription_url_groups"]
    )
    assert any(
        "https://www.javbus.com/star/actor-bulk-002" in group
        for group in res["report"]["subscription_url_groups"]
    )
    assert main_module.db.get_automation_candidate_by_id(first["id"])["status"] == "approved"
    assert main_module.db.get_automation_candidate_by_id(second["id"])["status"] == "approved"
    assert bool(main_module.db.get_actress("actor-bulk-001")["is_followed"]) is True
    assert bool(main_module.db.get_actress("actor-bulk-002")["is_followed"]) is True
    subscriptions = main_module.db.get_subscriptions()
    assert any(row["url"] == "https://www.javbus.com/star/actor-bulk-001" for row in subscriptions)
    assert any(row["url"] == "https://www.javbus.com/star/actor-bulk-002" for row in subscriptions)


def test_automation_modes_and_action_gates(main_module):
    from fastapi.testclient import TestClient

    client = TestClient(main_module.app)
    legacy = client.post(
        "/api/automation/settings",
        json={"jellyfin_auto_approve_candidates": True},
    )
    assert legacy.status_code == 200
    assert legacy.json()["data"]["automation_mode"] == "auto_approve_low_risk"
    assert legacy.json()["data"]["auto_follow_enabled"] is True
    assert legacy.json()["data"]["auto_subscribe_enabled"] is True

    suggest = client.post(
        "/api/automation/settings",
        json={
            "automation_mode": "suggest",
            "auto_follow_enabled": True,
            "auto_subscribe_enabled": True,
            "auto_download_enabled": True,
            "auto_cleanup_enabled": True,
        },
    )
    assert suggest.status_code == 200
    assert suggest.json()["data"]["jellyfin_auto_approve_candidates"] is False

    follow_candidate = main_module.db.upsert_automation_candidate(
        "favorite_actor", "actor-follow-only-001", "Follow Only Actor",
        "AUTO-MODE-001", "Follow Only Source", "scenario", "follow_actor",
        "action_gate_test", 0.9,
        {"actor_page_url": "https://www.javbus.com/star/follow-only-001"},
    )
    follow_result = main_module.get_jellyfin_service().apply_approved_automation_candidate(
        follow_candidate, follow=True, subscribe=False
    )
    assert follow_result["applied"] is True
    assert follow_result["followed"] is True
    assert follow_result["subscribed"] is False
    assert not any(
        row["url"] == "https://www.javbus.com/star/follow-only-001"
        for row in main_module.db.get_subscriptions()
    )

    subscribe_candidate = main_module.db.upsert_automation_candidate(
        "favorite_actor", "actor-subscribe-only-001", "Subscribe Only Actor",
        "AUTO-MODE-002", "Subscribe Only Source", "scenario", "subscribe_actor",
        "action_gate_test", 0.9,
        {"actor_page_url": "https://www.javbus.com/star/subscribe-only-001"},
    )
    subscribe_result = main_module.get_jellyfin_service().apply_approved_automation_candidate(
        subscribe_candidate, follow=False, subscribe=True
    )
    assert subscribe_result["applied"] is True
    assert subscribe_result["followed"] is False
    assert subscribe_result["subscribed"] is True
    assert bool(main_module.db.get_actress("actor-subscribe-only-001")["is_followed"]) is False

    invalid = client.post("/api/automation/settings", json={"automation_mode": "delete_everything"})
    assert invalid.status_code == 422


async def test_jellyfin_webhook_creates_candidates_only(main_module):
    main_module.db.set_config("automation_mode", "suggest")
    main_module.db.set_config("auto_follow_enabled", "false")
    main_module.db.set_config("auto_subscribe_enabled", "false")
    original_get_movie_detail = main_module.scraper.get_movie_detail

    def fake_get_movie_detail(url, *args, **kwargs):
        assert "WEB-001" in url
        return {
            "title": "Webhook Favorite",
            "stars": [
                {
                    "id": "actor-webhook-001",
                    "name": "Webhook Actor",
                    "url": "https://example.test/actor-webhook-001",
                }
            ],
        }

    before_queue_count = main_module.db.count_download_queue_since(0, automated_only=False)
    main_module.scraper.get_movie_detail = fake_get_movie_detail
    try:
        res = await main_module.jellyfin_webhook(
            main_module.JellyfinWebhookRequest(
                Name="WEB-001 [1080p]",
                NotificationType="UserDataSaved",
                UserData={"IsFavorite": True},
            )
        )
    finally:
        main_module.scraper.get_movie_detail = original_get_movie_detail

    assert res["success"] is True
    assert res["action"] == "created_automation_candidates"
    assert len(res["candidates"]) == 1

    rows = main_module.db.get_automation_candidates(status="candidate", candidate_type="favorite_actor")
    candidate = next(row for row in rows if row["subject_id"] == "actor-webhook-001")
    assert candidate["source_movie_id"] == "WEB-001"
    assert candidate["source"] == "jellyfin_favorite"
    assert main_module.db.get_actress("actor-webhook-001") is None
    assert not any(row["url"] == "https://example.test/actor-webhook-001" for row in main_module.db.get_subscriptions())
    assert main_module.db.count_download_queue_since(0, automated_only=False) == before_queue_count

    events = main_module.db.get_movie_lifecycle_events("WEB-001")
    assert any(event["event_type"] == "jellyfin_favorite_status_updated" for event in events)


def test_jellyfin_webhook_optional_token_guard(main_module):
    from fastapi.testclient import TestClient

    original_token = os.environ.get(main_module.JELLYFIN_WEBHOOK_TOKEN_ENV)
    os.environ[main_module.JELLYFIN_WEBHOOK_TOKEN_ENV] = "scenario-webhook-token"
    client = TestClient(main_module.app)
    try:
        missing = client.post(
            "/api/webhooks/jellyfin",
            json={"Name": "TOK-001", "NotificationType": "PlaybackStop", "PlayedToCompletion": True},
        )
        assert missing.status_code == 403
        assert missing.json()["detail"] == "invalid_jellyfin_webhook_token"

        allowed = client.post(
            "/api/webhooks/jellyfin",
            headers={"X-BusTerminal-Webhook-Token": "scenario-webhook-token"},
            json={"Name": "TOK-001", "NotificationType": "PlaybackStop", "PlayedToCompletion": True},
        )
        assert allowed.status_code == 200
        body = allowed.json()
        assert body["success"] is True
        assert body["action"] == "mark_watched"
    finally:
        if original_token is None:
            os.environ.pop(main_module.JELLYFIN_WEBHOOK_TOKEN_ENV, None)
        else:
            os.environ[main_module.JELLYFIN_WEBHOOK_TOKEN_ENV] = original_token


async def test_jellyfin_webhook_payload_variants(main_module):
    original_get_movie_detail = main_module.scraper.get_movie_detail

    def fake_get_movie_detail(url, *args, **kwargs):
        return {
            "title": "Direct Favorite",
            "stars": [
                {
                    "id": "actor-direct-001",
                    "name": "Direct Actor",
                    "url": "https://example.test/actor-direct-001",
                }
            ],
        }

    main_module.scraper.get_movie_detail = fake_get_movie_detail
    try:
        favorite_res = await main_module.jellyfin_webhook(
            main_module.JellyfinWebhookRequest(
                Name="DIR-001",
                NotificationType="ItemFavorite",
            )
        )
        playback_alias_res = await main_module.jellyfin_webhook(
            main_module.JellyfinWebhookRequest(
                Name="PLY-001",
                NotificationType="PlaybackStop",
                PlayedToCompletion=True,
            )
        )
        missing_name_res = await main_module.jellyfin_webhook(
            main_module.JellyfinWebhookRequest(
                NotificationType="ItemFavorite",
            )
        )
    finally:
        main_module.scraper.get_movie_detail = original_get_movie_detail

    assert favorite_res["success"] is True
    assert favorite_res["action"] == "created_automation_candidates"
    assert playback_alias_res["success"] is True
    assert playback_alias_res["action"] == "mark_watched"
    assert missing_name_res["success"] is False
    assert missing_name_res["message"] == "Missing movie id"

    direct_rows = main_module.db.get_automation_candidates(status="candidate", candidate_type="favorite_actor")
    assert any(row["source_movie_id"] == "DIR-001" for row in direct_rows)
    playback_events = main_module.db.get_movie_lifecycle_events("PLY-001")
    assert any(event["event_type"] == "jellyfin_watched" for event in playback_events)


async def test_jellyfin_person_favorite_creates_subscription(main_module):
    main_module.db.set_config("automation_mode", "suggest")
    main_module.db.set_config("auto_follow_enabled", "false")
    main_module.db.set_config("auto_subscribe_enabled", "false")
    res = await main_module.jellyfin_webhook(
        main_module.JellyfinWebhookRequest(
            Name="Webhook Person",
            ItemId="person-001",
            ItemType="Person",
            NotificationType="ItemFavorite",
            UserData={"IsFavorite": True},
        )
    )

    assert res["success"] is True
    assert res["action"] == "created_automation_candidates"
    assert res["application"] is None
    assert res["candidates"][0]["status"] == "candidate"
    assert main_module.db.get_actress("jf_person:person-001") is None
    assert not any(row["url"] == "jellyfin:person:person-001" for row in main_module.db.get_subscriptions())


async def test_jellyfin_favorite_people_sync_creates_subscription(main_module):
    original_reload_config = main_module.jellyfin_client.reload_config
    original_get_favorite_movies = main_module.jellyfin_client.get_favorite_movies
    original_get_favorite_people = main_module.jellyfin_client.get_favorite_people
    original_get_watched_movies = main_module.jellyfin_client.get_watched_movies
    original_host = main_module.jellyfin_client.host
    original_api_key = main_module.jellyfin_client.api_key

    main_module.jellyfin_client.reload_config = lambda: None
    main_module.jellyfin_client.host = "http://jellyfin.local"
    main_module.jellyfin_client.api_key = "scenario-key"
    main_module.jellyfin_client.get_favorite_movies = lambda: []
    main_module.jellyfin_client.get_favorite_people = lambda: [
        {"Id": "person-sync-001", "Name": "Synced Person"}
    ]
    main_module.jellyfin_client.get_watched_movies = lambda: []
    main_module.db.set_config("automation_mode", "suggest")
    main_module.db.set_config("auto_follow_enabled", "false")
    main_module.db.set_config("auto_subscribe_enabled", "false")

    try:
        res = await main_module.sync_jellyfin_status()
    finally:
        main_module.jellyfin_client.reload_config = original_reload_config
        main_module.jellyfin_client.get_favorite_movies = original_get_favorite_movies
        main_module.jellyfin_client.get_favorite_people = original_get_favorite_people
        main_module.jellyfin_client.get_watched_movies = original_get_watched_movies
        main_module.jellyfin_client.host = original_host
        main_module.jellyfin_client.api_key = original_api_key

    assert res["success"] is True
    assert res["report"]["favorite_people"] == 1
    assert res["report"]["favorite_people_subscribed"] == 0
    assert res["report"]["favorite_people_created"] == 0
    candidate = main_module.db.get_automation_candidate(
        "favorite_actor", "jf_person:person-sync-001", None
    )
    assert candidate["status"] == "candidate"
    assert main_module.db.get_actress("jf_person:person-sync-001") is None


def test_actress_subscribe_alias_creates_subscription(main_module):
    from fastapi.testclient import TestClient

    main_module.db.save_actress({
        "id": "actor-subscribe-alias-001",
        "name": "Subscribe Alias Actor",
        "page_url": "https://www.javbus.com/star/subscribe-alias-001",
        "is_followed": False,
        "source": "scenario",
    })

    client = TestClient(main_module.app)
    response = client.post("/api/actresses/subscribe", json={"actress_id": "actor-subscribe-alias-001"})

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["is_subscribed"] is True
    assert body["subscription_created"] is True
    assert body["subscription_url"] == "https://www.javbus.com/star/subscribe-alias-001"
    assert any(
        row["url"] == "https://www.javbus.com/star/subscribe-alias-001"
        and row["name"] == "Subscribe Alias Actor"
        and row["category_type"] == "star"
        for row in main_module.db.get_subscriptions()
    )


async def test_actress_subscribe_treats_string_false_as_unsubscribed(main_module):
    original_get_actress = main_module.db.get_actress
    original_add_subscription = main_module.db.add_subscription
    original_subscription_visible = main_module.subscription_visible_for_actress
    original_toggle_follow = main_module.db.toggle_follow_actress
    add_calls = []
    toggle_calls = []

    main_module.db.get_actress = lambda actress_id: {
        "id": actress_id,
        "name": "String False Subscribe Actor",
        "page_url": "https://www.javbus.com/star/string-false-subscribe",
        "is_followed": "false",
    }

    def fake_add_subscription(*args, **kwargs):
        add_calls.append((args, kwargs))
        return True

    def fake_toggle_follow(actress_id, force_state=None):
        toggle_calls.append((actress_id, force_state))
        return force_state

    main_module.db.add_subscription = fake_add_subscription
    main_module.subscription_visible_for_actress = lambda *args, **kwargs: True
    main_module.db.toggle_follow_actress = fake_toggle_follow

    try:
        res = await main_module.toggle_actress_subscription(
            main_module.ActressSubscriptionRequest(actress_id="string-false-subscribe-001")
        )
    finally:
        main_module.db.get_actress = original_get_actress
        main_module.db.add_subscription = original_add_subscription
        main_module.subscription_visible_for_actress = original_subscription_visible
        main_module.db.toggle_follow_actress = original_toggle_follow

    assert res["success"] is True
    assert res["is_subscribed"] is True
    assert res["subscribed"] is True
    assert len(add_calls) == 1
    assert toggle_calls == [("string-false-subscribe-001", True)]


def test_save_actress_parses_string_follow_state(main_module):
    assert main_module.db.save_actress({
        "id": "dirty-follow-false-001",
        "name": "Dirty Follow False",
        "is_followed": "false",
        "source": "scenario",
    })
    actress = main_module.db.get_actress("dirty-follow-false-001")
    assert bool(actress["is_followed"]) is False

    assert main_module.db.save_actress({
        "id": "dirty-follow-true-001",
        "name": "Dirty Follow True",
        "is_followed": "true",
        "source": "scenario",
    })
    actress = main_module.db.get_actress("dirty-follow-true-001")
    assert bool(actress["is_followed"]) is True

    toggled = main_module.db.toggle_follow_actress("dirty-follow-true-001", force_state="false")
    assert toggled is False
    actress = main_module.db.get_actress("dirty-follow-true-001")
    assert bool(actress["is_followed"]) is False


def test_actresses_endpoint_tolerates_dirty_timestamps(main_module):
    from fastapi.testclient import TestClient

    main_module.db.set_config("actresses_last_viewed_at", "not-a-timestamp")
    main_module.db.save_actress({
        "id": "actor-dirty-time-001",
        "name": "Dirty Time Actor",
        "source": "scenario",
    })
    conn = main_module.db.get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE actresses SET created_at = ? WHERE id = ?",
            ("also-not-a-timestamp", "actor-dirty-time-001"),
        )
        conn.commit()
    finally:
        main_module.db._release_connection(conn)

    client = TestClient(main_module.app)
    response = client.get("/api/actresses", params={"query": "Dirty Time Actor"})

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert any(
        row["id"] == "actor-dirty-time-001"
        and row["is_new"] is False
        for row in body["data"]
    )


async def test_jellyfin_active_sync_creates_candidates_only(main_module):
    main_module.db.set_config("automation_mode", "suggest")
    main_module.db.set_config("auto_follow_enabled", "false")
    main_module.db.set_config("auto_subscribe_enabled", "false")
    original_reload_config = main_module.jellyfin_client.reload_config
    original_get_favorite_movies = main_module.jellyfin_client.get_favorite_movies
    original_get_favorite_people = main_module.jellyfin_client.get_favorite_people
    original_get_watched_movies = main_module.jellyfin_client.get_watched_movies
    original_get_movie_detail = main_module.scraper.get_movie_detail
    original_host = main_module.jellyfin_client.host
    original_api_key = main_module.jellyfin_client.api_key
    # scraper.base_url 是进程级全局状态，别的用例或后台抓取线程可能把它切到别的镜像域名。
    # 本用例关心的是"详情 URL 由本地配置域名拼出、且带上番号"，所以把它钉住再断言。
    expected_base_url = main_module.scraper.base_url

    def fake_get_movie_detail(url, *args, **kwargs):
        assert url == f"{expected_base_url}/SYN-001"
        return {
            "title": "Sync Favorite",
            "stars": [
                {
                    "id": "actor-sync-001",
                    "name": "Sync Actor",
                    "url": "https://example.test/actor-sync-001",
                }
            ],
        }

    before_queue_count = main_module.db.count_download_queue_since(0, automated_only=False)
    main_module.jellyfin_client.reload_config = lambda: None
    main_module.jellyfin_client.host = "http://jellyfin.local"
    main_module.jellyfin_client.api_key = "scenario-key"
    main_module.jellyfin_client.get_favorite_movies = lambda: [{"Name": "SYN-001 Favorite"}]
    main_module.jellyfin_client.get_favorite_people = lambda: []
    main_module.jellyfin_client.get_watched_movies = lambda: [{"Name": "WAT-001 Watched"}]
    main_module.scraper.get_movie_detail = fake_get_movie_detail
    main_module.scraper.base_url = expected_base_url
    try:
        res = await main_module.sync_jellyfin_status()
    finally:
        main_module.scraper.base_url = expected_base_url
        main_module.jellyfin_client.reload_config = original_reload_config
        main_module.jellyfin_client.get_favorite_movies = original_get_favorite_movies
        main_module.jellyfin_client.get_favorite_people = original_get_favorite_people
        main_module.jellyfin_client.get_watched_movies = original_get_watched_movies
        main_module.jellyfin_client.host = original_host
        main_module.jellyfin_client.api_key = original_api_key
        main_module.scraper.get_movie_detail = original_get_movie_detail

    assert res["success"] is True
    assert res["report"]["candidates"] == 1
    assert res["report"]["watched"] == 1

    rows = main_module.db.get_automation_candidates(status="candidate", candidate_type="favorite_actor")
    candidate = next(row for row in rows if row["subject_id"] == "actor-sync-001")
    assert candidate["source_movie_id"] == "SYN-001"
    assert candidate["source"] == "jellyfin_favorite_sync"
    assert main_module.db.get_actress("actor-sync-001") is None
    assert not any(row["url"] == "https://example.test/actor-sync-001" for row in main_module.db.get_subscriptions())
    assert main_module.db.count_download_queue_since(0, automated_only=False) == before_queue_count

    watched_events = main_module.db.get_movie_lifecycle_events("WAT-001")
    assert any(event["event_type"] == "jellyfin_watched" for event in watched_events)


async def test_jellyfin_people_sync_auto_subscribes_known_actress(main_module):
    main_module.db.set_config("automation_mode", "auto_approve_low_risk")
    main_module.db.set_config("auto_follow_enabled", "true")
    main_module.db.set_config("auto_subscribe_enabled", "true")
    original_reload_config = main_module.jellyfin_client.reload_config
    original_get_favorite_movies = main_module.jellyfin_client.get_favorite_movies
    original_get_favorite_people = main_module.jellyfin_client.get_favorite_people
    original_get_watched_movies = main_module.jellyfin_client.get_watched_movies
    original_get_movie_detail = main_module.scraper.get_movie_detail
    original_host = main_module.jellyfin_client.host
    original_api_key = main_module.jellyfin_client.api_key

    main_module.db.save_actress({
        "id": "actor-people-001",
        "name": "Matched Actor",
        "page_url": "https://www.javbus.com/star/actor-people-001",
        "is_followed": False,
    })

    main_module.jellyfin_client.reload_config = lambda: None
    main_module.jellyfin_client.host = "http://jellyfin.local"
    main_module.jellyfin_client.api_key = "scenario-key"
    main_module.jellyfin_client.get_favorite_movies = lambda: [
        {
            "Name": "PEO-001 Favorite",
            "People": [{"Name": "Matched Actor", "Type": "Actor"}],
        }
    ]
    main_module.jellyfin_client.get_favorite_people = lambda: []
    main_module.jellyfin_client.get_watched_movies = lambda: []
    main_module.scraper.get_movie_detail = lambda *args, **kwargs: {"title": "People Favorite", "stars": []}

    try:
        res = await main_module.sync_jellyfin_status()
    finally:
        main_module.jellyfin_client.reload_config = original_reload_config
        main_module.jellyfin_client.get_favorite_movies = original_get_favorite_movies
        main_module.jellyfin_client.get_favorite_people = original_get_favorite_people
        main_module.jellyfin_client.get_watched_movies = original_get_watched_movies
        main_module.jellyfin_client.host = original_host
        main_module.jellyfin_client.api_key = original_api_key
        main_module.scraper.get_movie_detail = original_get_movie_detail

    assert res["success"] is True
    assert res["report"]["auto_followed"] >= 1
    assert res["report"]["auto_subscribed"] >= 1
    actress = main_module.db.get_actress("actor-people-001")
    assert bool(actress["is_followed"]) is True
    assert any(row["url"] == "https://www.javbus.com/star/actor-people-001" for row in main_module.db.get_subscriptions())


async def test_jellyfin_favorite_actor_creates_subscription(main_module):
    original_get_item_by_name = main_module.jellyfin_client.get_item_by_name
    original_toggle_favorite_item = main_module.jellyfin_client.toggle_favorite_item

    main_module.jellyfin_client.get_item_by_name = lambda *args, **kwargs: None
    main_module.jellyfin_client.toggle_favorite_item = lambda *args, **kwargs: True

    try:
        res = await main_module.favorite_actor_sync({
            "name": "Jellyfin Actor",
            "favorite": True,
        })
    finally:
        main_module.jellyfin_client.get_item_by_name = original_get_item_by_name
        main_module.jellyfin_client.toggle_favorite_item = original_toggle_favorite_item

    assert res["success"] is True
    assert res["is_followed"] is True
    assert res["subscribed"] is True
    assert res["subscription_visible"] is True

    subscriptions = main_module.db.get_subscriptions()
    assert any(
        row["url"] == "jellyfin:actor:Jellyfin Actor"
        and row["name"] == "Jellyfin Actor"
        and row["category_type"] == "star"
        for row in subscriptions
    )


async def test_jellyfin_favorite_actor_string_false_unsubscribes(main_module):
    original_get_item_by_name = main_module.jellyfin_client.get_item_by_name
    original_toggle_favorite_item = main_module.jellyfin_client.toggle_favorite_item

    main_module.db.save_actress({
        "id": "actor-string-false-001",
        "name": "String False Actor",
        "page_url": "https://www.javbus.com/star/string-false-001",
        "is_followed": True,
        "source": "scenario",
    })
    assert main_module.db.add_subscription(
        "https://www.javbus.com/star/string-false-001",
        "String False Actor",
        "star",
        "2026-01-01",
    )

    main_module.jellyfin_client.get_item_by_name = lambda *args, **kwargs: None
    main_module.jellyfin_client.toggle_favorite_item = lambda *args, **kwargs: True

    try:
        res = await main_module.favorite_actor_sync({
            "name": "String False Actor",
            "actress_id": "actor-string-false-001",
            "favorite": "false",
        })
    finally:
        main_module.jellyfin_client.get_item_by_name = original_get_item_by_name
        main_module.jellyfin_client.toggle_favorite_item = original_toggle_favorite_item

    assert res["success"] is True
    assert res["is_subscribed"] is False
    assert res["subscribed"] is False
    assert bool(main_module.db.get_actress("actor-string-false-001")["is_followed"]) is False
    assert not any(
        row["url"] == "https://www.javbus.com/star/string-false-001"
        for row in main_module.db.get_subscriptions()
    )


async def test_jellyfin_favorite_actor_string_false_removes_legacy_alias_subscriptions(main_module):
    original_get_item_by_name = main_module.jellyfin_client.get_item_by_name
    original_toggle_favorite_item = main_module.jellyfin_client.toggle_favorite_item

    main_module.db.save_actress({
        "id": "jf_person:alias-unsub-001",
        "name": "Alias Unsubscribe Actor",
        "is_followed": True,
        "source": "jellyfin",
    })
    assert main_module.db.add_subscription(
        "jellyfin:person:alias-unsub-001",
        "Alias Unsubscribe Actor",
        "star",
        "2026-01-01",
    )
    assert main_module.db.add_subscription(
        "jf_person:alias-unsub-001",
        "Alias Unsubscribe Actor",
        "star",
        "2026-01-01",
    )

    main_module.jellyfin_client.get_item_by_name = lambda *args, **kwargs: None
    main_module.jellyfin_client.toggle_favorite_item = lambda *args, **kwargs: True

    try:
        res = await main_module.favorite_actor_sync({
            "name": "Alias Unsubscribe Actor",
            "actress_id": "jf_person:alias-unsub-001",
            "source_url": "jellyfin:person:alias-unsub-001",
            "favorite": "false",
        })
    finally:
        main_module.jellyfin_client.get_item_by_name = original_get_item_by_name
        main_module.jellyfin_client.toggle_favorite_item = original_toggle_favorite_item

    assert res["success"] is True
    assert res["is_subscribed"] is False
    remaining_urls = {row["url"] for row in main_module.db.get_subscriptions()}
    assert "jellyfin:person:alias-unsub-001" not in remaining_urls
    assert "jf_person:alias-unsub-001" not in remaining_urls


def test_jellyfin_favorite_state_parses_string_booleans(main_module):
    assert main_module.jellyfin_favorite_state(
        main_module.JellyfinWebhookRequest(UserData={"IsFavorite": "false"})
    ) is False
    assert main_module.jellyfin_favorite_state(
        main_module.JellyfinWebhookRequest(UserData={"IsFavorite": "true"})
    ) is True


async def test_jellyfin_favorite_actor_ignores_unsafe_external_subscription_url(main_module):
    original_get_item_by_name = main_module.jellyfin_client.get_item_by_name
    original_toggle_favorite_item = main_module.jellyfin_client.toggle_favorite_item

    main_module.jellyfin_client.get_item_by_name = lambda *args, **kwargs: None
    main_module.jellyfin_client.toggle_favorite_item = lambda *args, **kwargs: True

    try:
        res = await main_module.favorite_actor_sync({
            "name": "Unsafe External Actor",
            "page_url": "http://127.0.0.1/private/star",
            "source_url": "http://127.0.0.1/private/source",
            "favorite": True,
        })
    finally:
        main_module.jellyfin_client.get_item_by_name = original_get_item_by_name
        main_module.jellyfin_client.toggle_favorite_item = original_toggle_favorite_item

    assert res["success"] is True
    assert res["subscription_visible"] is True
    assert res["subscription_url"] == "jellyfin:actor:Unsafe External Actor"
    assert not any("127.0.0.1" in str(row.get("url")) for row in main_module.db.get_subscriptions())
    actress = main_module.db.get_actress("jf_actor:Unsafe External Actor")
    assert not (actress or {}).get("page_url")


async def test_jellyfin_favorite_actor_is_visible_in_subscriptions_endpoint(main_module):
    original_get_item_by_name = main_module.jellyfin_client.get_item_by_name
    original_toggle_favorite_item = main_module.jellyfin_client.toggle_favorite_item

    main_module.jellyfin_client.get_item_by_name = lambda *args, **kwargs: None
    main_module.jellyfin_client.toggle_favorite_item = lambda *args, **kwargs: True

    try:
        res = await main_module.favorite_actor_sync({
            "name": "Endpoint Visible Actor",
            "favorite": True,
        })
        subscriptions_res = await main_module.get_subscriptions()
    finally:
        main_module.jellyfin_client.get_item_by_name = original_get_item_by_name
        main_module.jellyfin_client.toggle_favorite_item = original_toggle_favorite_item

    assert res["success"] is True
    assert res["subscription_visible"] is True
    assert subscriptions_res["success"] is True
    assert any(
        row["url"] == res["subscription_url"]
        and row["name"] == "Endpoint Visible Actor"
        and row["category_type"] == "star"
        for row in subscriptions_res["data"]
    )


def test_jellyfin_favorite_actor_route_is_visible_in_subscriptions_page(main_module):
    from fastapi.testclient import TestClient

    original_get_item_by_name = main_module.jellyfin_client.get_item_by_name
    original_toggle_favorite_item = main_module.jellyfin_client.toggle_favorite_item

    main_module.jellyfin_client.get_item_by_name = lambda *args, **kwargs: None
    main_module.jellyfin_client.toggle_favorite_item = lambda *args, **kwargs: True

    client = TestClient(main_module.app)
    try:
        favorite_res = client.post("/api/jellyfin/favorite_actor", json={
            "name": "Route Visible Actor",
            "favorite": True,
        })
        subscriptions_res = client.get("/api/subscriptions")
    finally:
        main_module.jellyfin_client.get_item_by_name = original_get_item_by_name
        main_module.jellyfin_client.toggle_favorite_item = original_toggle_favorite_item

    assert favorite_res.status_code == 200
    favorite_body = favorite_res.json()
    assert favorite_body["success"] is True
    assert favorite_body["is_subscribed"] is True
    assert favorite_body["subscription_visible"] is True
    assert favorite_body["subscription_url"] in favorite_body["subscription_urls"]

    assert subscriptions_res.status_code == 200
    subscriptions_body = subscriptions_res.json()
    assert subscriptions_body["success"] is True
    assert any(
        row["url"] == favorite_body["subscription_url"]
        and row["name"] == "Route Visible Actor"
        and row["category_type"] == "star"
        and row["is_subscribed"] is True
        for row in subscriptions_body["data"]
    )


async def test_jellyfin_person_source_url_is_visible_in_subscriptions_endpoint(main_module):
    original_get_item_by_name = main_module.jellyfin_client.get_item_by_name
    original_toggle_favorite_item = main_module.jellyfin_client.toggle_favorite_item

    main_module.jellyfin_client.get_item_by_name = lambda *args, **kwargs: None
    main_module.jellyfin_client.toggle_favorite_item = lambda *args, **kwargs: True

    try:
        res = await main_module.favorite_actor_sync({
            "name": "Person Source Actor",
            "actor_id": "jf_person:source-visible-001",
            "source_url": "jellyfin:person:source-visible-001",
            "favorite": True,
        })
        subscriptions_res = await main_module.get_subscriptions()
    finally:
        main_module.jellyfin_client.get_item_by_name = original_get_item_by_name
        main_module.jellyfin_client.toggle_favorite_item = original_toggle_favorite_item

    assert res["success"] is True
    assert res["subscription_visible"] is True
    assert res["subscription_url"] == "jellyfin:person:source-visible-001"
    assert any(
        row["url"] == "jellyfin:person:source-visible-001"
        and row["name"] == "Person Source Actor"
        and row["category_type"] == "star"
        for row in subscriptions_res["data"]
    )


async def test_jellyfin_favorite_actor_source_url_without_id_is_visible(main_module):
    original_get_item_by_name = main_module.jellyfin_client.get_item_by_name
    original_toggle_favorite_item = main_module.jellyfin_client.toggle_favorite_item

    main_module.jellyfin_client.get_item_by_name = lambda *args, **kwargs: None
    main_module.jellyfin_client.toggle_favorite_item = lambda *args, **kwargs: True

    try:
        res = await main_module.favorite_actor_sync({
            "name": "Source Only Person",
            "source_url": "jellyfin:person:source-only-001",
            "favorite": True,
        })
        subscriptions_res = await main_module.get_subscriptions()
    finally:
        main_module.jellyfin_client.get_item_by_name = original_get_item_by_name
        main_module.jellyfin_client.toggle_favorite_item = original_toggle_favorite_item

    assert res["success"] is True
    assert res["actress_id"] == "jf_person:source-only-001"
    assert res["subscription_visible"] is True
    assert res["subscription_url"] == "jellyfin:person:source-only-001"
    assert any(
        row["url"] == "jellyfin:person:source-only-001"
        and row["name"] == "Source Only Person"
        and row["category_type"] == "star"
        for row in subscriptions_res["data"]
    )


def test_subscription_visibility_accepts_legacy_actor_urls(main_module):
    main_module.db.save_actress({
        "id": "actor-visible-legacy",
        "name": "Visible Legacy Actor",
        "page_url": "https://example.test/star/actor-visible-legacy",
        "is_followed": True,
        "source": "scenario",
    })
    assert main_module.db.add_subscription(
        "jellyfin:actress:actor-visible-legacy",
        "Visible Legacy Actor",
        "star",
        "2026-01-01",
    )

    assert main_module.subscription_visible_for_actress(
        "actor-visible-legacy",
        "https://example.test/star/actor-visible-legacy",
    ) is True


def test_subscription_visibility_accepts_jellyfin_actor_urls(main_module):
    main_module.db.save_actress({
        "id": "actor-visible-jellyfin",
        "name": "Visible Jellyfin Actor",
        "is_followed": True,
        "source": "jellyfin",
    })
    assert main_module.db.add_subscription(
        "jellyfin:actor:actor-visible-jellyfin",
        "Visible Jellyfin Actor",
        "star",
        "2026-01-01",
    )

    assert main_module.subscription_visible_for_actress("actor-visible-jellyfin") is True


def test_subscription_visibility_accepts_jellyfin_person_legacy_equivalents(main_module):
    main_module.db.save_actress({
        "id": "jellyfin:person:person-visible-equivalent",
        "name": "Visible Jellyfin Person",
        "is_followed": True,
        "source": "jellyfin",
    })
    assert main_module.db.add_subscription(
        "jf_person:person-visible-equivalent",
        "Visible Jellyfin Person",
        "star",
        "2026-01-01",
    )

    assert main_module.subscription_visible_for_actress("jellyfin:person:person-visible-equivalent") is True


def test_subscription_url_candidates_include_jellyfin_equivalents(main_module):
    person_urls = main_module.get_actress_subscription_url_candidates(
        "jf_person:person-candidate-equivalent",
        subscription_url="jellyfin:person:person-candidate-equivalent",
    )
    actor_urls = main_module.get_actress_subscription_url_candidates(
        "jf_actor:Actor Candidate Equivalent",
        subscription_url="jellyfin:actor:Actor Candidate Equivalent",
    )
    legacy_source_urls = main_module.get_actress_subscription_url_candidates(
        None,
        source_url="jf_person:legacy-source-candidate",
        subscription_url="jf_actor:Legacy Source Actor",
    )

    assert "jellyfin:person:person-candidate-equivalent" in person_urls
    assert "jellyfin:actor:Actor Candidate Equivalent" in actor_urls
    assert "jellyfin:person:legacy-source-candidate" in legacy_source_urls
    assert "jf_person:legacy-source-candidate" in legacy_source_urls
    assert "jellyfin:actor:Legacy Source Actor" in legacy_source_urls
    assert "jf_actor:Legacy Source Actor" in legacy_source_urls
    assert len(person_urls) == len(set(person_urls))
    assert len(actor_urls) == len(set(actor_urls))
    assert len(legacy_source_urls) == len(set(legacy_source_urls))


def test_internal_subscription_url_guardrails(main_module):
    from fastapi.testclient import TestClient

    assert (
        main_module.normalize_internal_subscription_url("JELLYFIN:PERSON:Person 001")
        == "jellyfin:person:Person 001"
    )
    assert main_module.normalize_internal_subscription_url("jellyfin:person:../secret") is None
    assert main_module.normalize_internal_subscription_url("jellyfin:actor:bad/name") is None
    assert main_module.normalize_internal_subscription_url("jellyfin:actress:bad?api_key=secret") is None
    assert main_module.normalize_subscription_source_url("jellyfin:person:../secret") is None

    client = TestClient(main_module.app)
    invalid = client.post(
        "/api/subscriptions",
        json={
            "url": "jellyfin:person:../secret",
            "name": "Bad Internal",
            "category_type": "star",
            "start_date": "2026-01-01",
        },
    )
    assert invalid.status_code == 200
    assert invalid.json()["success"] is False
    assert invalid.json()["message"] == "invalid_subscription_url"

    valid = client.post(
        "/api/subscriptions",
        json={
            "url": "jellyfin:actor:Actor Name",
            "name": "Actor Name",
            "category_type": "star",
            "start_date": "2026-01-01",
        },
    )
    assert valid.status_code == 200
    assert valid.json()["success"] is True
    assert any(row["url"] == "jellyfin:actor:Actor Name" for row in main_module.db.get_subscriptions())
    assert not any(row["url"] == "jellyfin:person:../secret" for row in main_module.db.get_subscriptions())

    assert main_module.db.add_subscription(
        "jf_person:Legacy Delete Person",
        "Legacy Delete Person",
        "star",
        "2026-01-01",
    )
    assert main_module.db.add_subscription(
        "jellyfin:person:Legacy Delete Person",
        "Legacy Delete Person",
        "star",
        "2026-01-01",
    )
    delete_legacy = client.delete(
        "/api/subscriptions",
        params={"url": "jf_person:Legacy Delete Person"},
    )
    assert delete_legacy.status_code == 200
    assert delete_legacy.json()["success"] is True
    remaining_urls = {row["url"] for row in main_module.db.get_subscriptions()}
    assert "jf_person:Legacy Delete Person" not in remaining_urls
    assert "jellyfin:person:Legacy Delete Person" not in remaining_urls


def test_subscription_refresh_accepts_internal_alias_without_scraping(main_module):
    from fastapi.testclient import TestClient

    original_get_movies = main_module.scraper.get_movies
    scrape_calls = []

    def fail_get_movies(*args, **kwargs):
        scrape_calls.append((args, kwargs))
        raise AssertionError("internal Jellyfin subscriptions should not be scraped")

    assert main_module.db.add_subscription(
        "jellyfin:person:Refresh Alias Person",
        "Refresh Alias Person",
        "star",
        "2026-01-01",
    )

    main_module.scraper.get_movies = fail_get_movies
    try:
        client = TestClient(main_module.app)
        response = client.post(
            "/api/subscriptions/refresh",
            params={"url": "jf_person:Refresh Alias Person"},
        )
    finally:
        main_module.scraper.get_movies = original_get_movies

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["report"]["added_count"] == 0
    assert body["report"]["logs"][0]["status"] == "skipped"
    # 内部女优订阅先尝试解析真实来源；解析失败时跳过，且绝不触发抓取
    assert body["report"]["logs"][0]["reason"] == "actress_url_unresolved"
    assert scrape_calls == []


async def test_subscriptions_endpoint_repairs_raw_jellyfin_person_id(main_module):
    main_module.db.save_actress({
        "id": "jellyfin:person:raw-person-001",
        "name": "订阅: Raw Person Actor",
        "is_followed": True,
        "source": "jellyfin",
    })

    res = await main_module.get_subscriptions()

    assert res["success"] is True
    assert any(
        row["url"] == "jellyfin:person:raw-person-001"
        and row["name"] == "Raw Person Actor"
        and row["category_type"] == "star"
        and row["is_subscribed"] is True
        for row in res["data"]
    )


async def test_subscriptions_endpoint_returns_when_jellyfin_sync_times_out(main_module):
    original_timeout = main_module.SUBSCRIPTION_JELLYFIN_SYNC_TIMEOUT
    original_people_sync = main_module.sync_jellyfin_favorite_people_to_subscriptions
    original_movie_people_sync = main_module.sync_jellyfin_favorite_movie_people_to_subscriptions

    def slow_sync():
        time.sleep(0.1)
        return {"synced": 99, "subscribed": 99, "created": 99, "skipped": 0}

    main_module.SUBSCRIPTION_JELLYFIN_SYNC_TIMEOUT = 0.01
    main_module.sync_jellyfin_favorite_people_to_subscriptions = slow_sync
    main_module.sync_jellyfin_favorite_movie_people_to_subscriptions = slow_sync
    try:
        res = await main_module.get_subscriptions()
    finally:
        main_module.SUBSCRIPTION_JELLYFIN_SYNC_TIMEOUT = original_timeout
        main_module.sync_jellyfin_favorite_people_to_subscriptions = original_people_sync
        main_module.sync_jellyfin_favorite_movie_people_to_subscriptions = original_movie_people_sync

    assert res["success"] is True
    assert res["jellyfin_sync"]["favorite_people"]["error"] == "timeout"
    assert res["jellyfin_sync"]["favorite_movie_people"]["error"] == "timeout"


async def test_subscriptions_endpoint_tolerates_dirty_sync_counts(main_module):
    original_people_sync = main_module.sync_jellyfin_favorite_people_to_subscriptions
    original_movie_people_sync = main_module.sync_jellyfin_favorite_movie_people_to_subscriptions

    main_module.sync_jellyfin_favorite_people_to_subscriptions = lambda: {
        "synced": "dirty",
        "subscribed": "2",
        "created": None,
        "skipped": -1,
    }
    main_module.sync_jellyfin_favorite_movie_people_to_subscriptions = lambda: {
        "matched": "3",
        "subscribed": "dirty",
        "created": "1",
        "skipped": "bad",
    }
    try:
        res = await main_module.get_subscriptions()
    finally:
        main_module.sync_jellyfin_favorite_people_to_subscriptions = original_people_sync
        main_module.sync_jellyfin_favorite_movie_people_to_subscriptions = original_movie_people_sync

    assert res["success"] is True
    assert res["jellyfin_sync"]["synced"] == 3
    assert res["jellyfin_sync"]["subscribed"] == 2
    assert res["jellyfin_sync"]["created"] == 1
    assert res["jellyfin_sync"]["skipped"] == 0


async def test_jellyfin_favorite_actor_endpoint_uses_subscription_helper(main_module):
    original_helper = main_module.apply_jellyfin_actor_subscription
    calls = []

    async def fake_helper(**kwargs):
        calls.append(kwargs)
        return {
            "success": True,
            "is_followed": kwargs["is_subscribed"],
            "subscribed": kwargs["is_subscribed"],
            "actress_id": kwargs["actress_id"],
            "subscription_url": "jellyfin:actress:helper-001",
        }

    main_module.apply_jellyfin_actor_subscription = fake_helper
    try:
        res = await main_module.favorite_actor_sync({
            "name": "Helper Actor",
            "actress_id": "helper-001",
            "page_url": "https://www.javbus.com/star/helper-001",
            "favorite": True,
        })
    finally:
        main_module.apply_jellyfin_actor_subscription = original_helper

    assert res["success"] is True
    assert calls == [{
        "name": "Helper Actor",
        "actress_id": "helper-001",
        "page_url": "https://www.javbus.com/star/helper-001",
        "source_url": None,
        "is_subscribed": True,
        "sync_jellyfin_person": True,
    }]


async def test_subscriptions_endpoint_repairs_jellyfin_actor_subscription(main_module):
    main_module.db.save_actress({
        "id": "jf_person:repair-001",
        "name": "关注: Repair Person",
        "is_followed": True,
        "source": "jellyfin",
    })

    actress = main_module.db.get_actress("jf_person:repair-001")
    assert bool(actress["is_followed"]) is True
    assert not any(
        row["url"] == "jellyfin:person:repair-001"
        for row in main_module.db.get_subscriptions()
    )

    res = await main_module.get_subscriptions()

    assert res["success"] is True
    assert any(
        row["url"] == "jellyfin:person:repair-001"
        and row["name"] == "Repair Person"
        and row["category_type"] == "star"
        for row in res["data"]
    )


async def test_subscriptions_endpoint_repairs_jellyfin_actor_fallback_subscription(main_module):
    main_module.db.save_actress({
        "id": "jf_actor:Fallback Actor",
        "name": "关注: Fallback Actor",
        "is_followed": True,
        "source": "jellyfin",
    })

    res = await main_module.get_subscriptions()

    assert res["success"] is True
    assert any(
        row["url"] == "jellyfin:actor:Fallback Actor"
        and row["name"] == "Fallback Actor"
        and row["category_type"] == "star"
        for row in res["data"]
    )


async def test_subscriptions_endpoint_normalizes_legacy_subscription_names(main_module):
    assert main_module.db.add_subscription(
        "https://example.test/star/legacy-prefix-001",
        "关注: Legacy Prefix Actor",
        "star",
        "2026-01-01",
    )
    assert main_module.db.add_subscription(
        "jellyfin:person:legacy-prefix-002",
        "订阅: Jellyfin Prefix Actor",
        "star",
        "2026-01-01",
    )
    assert main_module.db.add_subscription(
        "jellyfin:person:legacy-mojibake-003",
        "\u00e8\u00b5\u00a4\u00e5\u009f\u008e\u00e7\u00a9\u0082\u00e6\u00b3\u00a2",
        "star",
        "2026-01-01",
    )

    res = await main_module.get_subscriptions()

    assert res["success"] is True
    assert res["name_normalization"]["normalized"] >= 3
    assert any(
        row["url"] == "https://example.test/star/legacy-prefix-001"
        and row["name"] == "Legacy Prefix Actor"
        and row["category_type"] == "star"
        for row in res["data"]
    )
    assert any(
        row["url"] == "jellyfin:person:legacy-prefix-002"
        and row["name"] == "Jellyfin Prefix Actor"
        and row["category_type"] == "star"
        for row in res["data"]
    )
    assert any(
        row["url"] == "jellyfin:person:legacy-mojibake-003"
        and row["name"] == "\u8d64\u57ce\u7a42\u6ce2"
        and row["category_type"] == "star"
        for row in res["data"]
    )


async def test_subscriptions_endpoint_syncs_jellyfin_favorite_people(main_module):
    original_reload_config = main_module.jellyfin_client.reload_config
    original_get_favorite_people = main_module.jellyfin_client.get_favorite_people
    original_get_favorite_movies = main_module.jellyfin_client.get_favorite_movies
    original_host = main_module.jellyfin_client.host
    original_api_key = main_module.jellyfin_client.api_key

    main_module.jellyfin_client.reload_config = lambda: None
    main_module.jellyfin_client.host = "http://jellyfin.local"
    main_module.jellyfin_client.api_key = "scenario-key"
    main_module.jellyfin_client.get_favorite_people = lambda: [
        {"Id": "person-subscriptions-001", "Name": "Subscription Page Person"}
    ]
    main_module.jellyfin_client.get_favorite_movies = lambda: []
    main_module.db.set_config("automation_mode", "suggest")

    try:
        res = await main_module.get_subscriptions()
    finally:
        main_module.jellyfin_client.reload_config = original_reload_config
        main_module.jellyfin_client.get_favorite_people = original_get_favorite_people
        main_module.jellyfin_client.get_favorite_movies = original_get_favorite_movies
        main_module.jellyfin_client.host = original_host
        main_module.jellyfin_client.api_key = original_api_key

    assert res["success"] is True
    assert res["jellyfin_sync"]["synced"] == 1
    assert res["jellyfin_sync"]["subscribed"] == 0
    assert not any(row["url"] == "jellyfin:person:person-subscriptions-001" for row in res["data"])
    assert main_module.db.get_automation_candidate(
        "favorite_actor", "jf_person:person-subscriptions-001", None
    )["status"] == "candidate"


async def test_jellyfin_person_candidate_approval_is_visible_in_subscriptions(main_module):
    candidate = main_module.db.upsert_automation_candidate(
        candidate_type="favorite_actor",
        subject_id="jf_person:candidate-visible-001",
        subject_name="Candidate Visible Person",
        source_movie_id="JFP-001",
        source_title="Jellyfin Favorite Source",
        source="jellyfin_favorite",
        recommended_action="review_follow_and_subscribe",
        confidence=0.9,
        reason="favorite_person",
        payload={"source_url": "jellyfin:person:candidate-visible-001"},
    )

    approval = await main_module.approve_automation_candidate(
        candidate["id"],
        main_module.AutomationCandidateDecisionRequest(actor="scenario", reason="subscribe person"),
    )
    subscriptions_res = await main_module.get_subscriptions()

    assert approval["success"] is True
    assert approval["application"]["subscription_visible"] is True
    assert approval["application"]["subscription_url"] == "jellyfin:person:candidate-visible-001"
    assert any(
        row["url"] == "jellyfin:person:candidate-visible-001"
        and row["name"] == "Candidate Visible Person"
        and row["category_type"] == "star"
        for row in subscriptions_res["data"]
    )


async def test_automation_candidate_approval_ignores_unsafe_external_subscription_url(main_module):
    candidate = main_module.db.upsert_automation_candidate(
        candidate_type="favorite_actor",
        subject_id="actor-unsafe-url-001",
        subject_name="Unsafe Candidate Actor",
        source_movie_id="UNSAFE-001",
        source_title="Unsafe Favorite Source",
        source="jellyfin_favorite",
        recommended_action="review_follow_and_subscribe",
        confidence=0.9,
        reason="favorite_person",
        payload={
            "actor_page_url": "http://127.0.0.1/private/star",
            "source_url": "http://127.0.0.1/private/source",
        },
    )

    approval = await main_module.approve_automation_candidate(
        candidate["id"],
        main_module.AutomationCandidateDecisionRequest(actor="scenario", reason="subscribe safe fallback"),
    )
    subscriptions_res = await main_module.get_subscriptions()

    assert approval["success"] is True
    assert approval["application"]["subscription_visible"] is True
    assert approval["application"]["subscription_url"] == "jellyfin:actress:actor-unsafe-url-001"
    assert not any("127.0.0.1" in str(row.get("url")) for row in subscriptions_res["data"])
    actress = main_module.db.get_actress("actor-unsafe-url-001")
    assert not (actress or {}).get("page_url")


async def test_automation_candidate_approval_ignores_non_scraper_external_subscription_url(main_module):
    candidate = main_module.db.upsert_automation_candidate(
        candidate_type="favorite_actor",
        subject_id="actor-non-scraper-url-001",
        subject_name="Non Scraper Candidate Actor",
        source_movie_id="NON-SCRAPER-001",
        source_title="Non Scraper Favorite Source",
        source="jellyfin_favorite",
        recommended_action="review_follow_and_subscribe",
        confidence=0.9,
        reason="favorite_person",
        payload={
            "actor_page_url": "https://evil.example/star/actor-non-scraper-url-001",
            "source_url": "https://evil.example/source/actor-non-scraper-url-001",
        },
    )

    approval = await main_module.approve_automation_candidate(
        candidate["id"],
        main_module.AutomationCandidateDecisionRequest(actor="scenario", reason="subscribe fallback"),
    )
    subscriptions_res = await main_module.get_subscriptions()

    assert approval["success"] is True
    assert approval["application"]["subscription_visible"] is True
    assert approval["application"]["subscription_url"] == "jellyfin:actress:actor-non-scraper-url-001"
    assert not any("evil.example" in str(row.get("url")) for row in subscriptions_res["data"])
    actress = main_module.db.get_actress("actor-non-scraper-url-001")
    assert not (actress or {}).get("page_url")


async def test_subscriptions_endpoint_syncs_jellyfin_favorite_movie_people(main_module):
    original_reload_config = main_module.jellyfin_client.reload_config
    original_get_favorite_people = main_module.jellyfin_client.get_favorite_people
    original_get_favorite_movies = main_module.jellyfin_client.get_favorite_movies
    original_host = main_module.jellyfin_client.host
    original_api_key = main_module.jellyfin_client.api_key

    main_module.db.save_actress({
        "id": "actor-subscriptions-movie-001",
        "name": "Movie Favorite Actor",
        "page_url": "https://www.javbus.com/star/movie-favorite-actor",
        "is_followed": False,
        "source": "scenario",
    })

    main_module.jellyfin_client.reload_config = lambda: None
    main_module.jellyfin_client.host = "http://jellyfin.local"
    main_module.jellyfin_client.api_key = "scenario-key"
    main_module.jellyfin_client.get_favorite_people = lambda: []
    main_module.jellyfin_client.get_favorite_movies = lambda: [
        {
            "Name": "FAV-001 Favorite Movie",
            "People": [{"Name": "Movie Favorite Actor", "Type": "Actor"}],
        }
    ]
    main_module.db.set_config("automation_mode", "suggest")

    try:
        res = await main_module.get_subscriptions()
    finally:
        main_module.jellyfin_client.reload_config = original_reload_config
        main_module.jellyfin_client.get_favorite_people = original_get_favorite_people
        main_module.jellyfin_client.get_favorite_movies = original_get_favorite_movies
        main_module.jellyfin_client.host = original_host
        main_module.jellyfin_client.api_key = original_api_key

    assert res["success"] is True
    assert res["jellyfin_sync"]["synced"] == 1
    assert res["jellyfin_sync"]["subscribed"] == 0
    assert res["jellyfin_sync"]["created"] == 0
    assert res["jellyfin_sync"]["favorite_movie_people"]["matched"] == 1
    assert not any(row["url"] == "https://www.javbus.com/star/movie-favorite-actor" for row in res["data"])
    assert main_module.db.get_automation_candidate(
        "favorite_actor", "actor-subscriptions-movie-001", "FAV-001"
    )["status"] == "candidate"


async def test_subscriptions_endpoint_syncs_known_jellyfin_person_without_page_url(main_module):
    original_reload_config = main_module.jellyfin_client.reload_config
    original_get_favorite_people = main_module.jellyfin_client.get_favorite_people
    original_get_favorite_movies = main_module.jellyfin_client.get_favorite_movies
    original_host = main_module.jellyfin_client.host
    original_api_key = main_module.jellyfin_client.api_key

    main_module.db.save_actress({
        "id": "local-known-person-001",
        "name": "Known Person Without Page",
        "is_followed": False,
        "source": "scenario",
    })

    main_module.jellyfin_client.reload_config = lambda: None
    main_module.jellyfin_client.host = "http://jellyfin.local"
    main_module.jellyfin_client.api_key = "scenario-key"
    main_module.jellyfin_client.get_favorite_people = lambda: []
    main_module.jellyfin_client.get_favorite_movies = lambda: [
        {
            "Name": "FAV-003 Known Person Movie",
            "People": [{"Id": "known-person-001", "Name": "Known Person Without Page", "Type": "Actor"}],
        }
    ]
    main_module.db.set_config("automation_mode", "suggest")

    try:
        res = await main_module.get_subscriptions()
    finally:
        main_module.jellyfin_client.reload_config = original_reload_config
        main_module.jellyfin_client.get_favorite_people = original_get_favorite_people
        main_module.jellyfin_client.get_favorite_movies = original_get_favorite_movies
        main_module.jellyfin_client.host = original_host
        main_module.jellyfin_client.api_key = original_api_key

    assert res["success"] is True
    assert res["jellyfin_sync"]["subscribed"] == 0
    assert not any(row["url"] == "jellyfin:person:known-person-001" for row in res["data"])
    assert main_module.db.get_automation_candidate(
        "favorite_actor", "local-known-person-001", "FAV-003"
    )["status"] == "candidate"


async def test_subscriptions_endpoint_syncs_unknown_jellyfin_movie_people(main_module):
    original_reload_config = main_module.jellyfin_client.reload_config
    original_get_favorite_people = main_module.jellyfin_client.get_favorite_people
    original_get_favorite_movies = main_module.jellyfin_client.get_favorite_movies
    original_host = main_module.jellyfin_client.host
    original_api_key = main_module.jellyfin_client.api_key

    main_module.jellyfin_client.reload_config = lambda: None
    main_module.jellyfin_client.host = "http://jellyfin.local"
    main_module.jellyfin_client.api_key = "scenario-key"
    main_module.jellyfin_client.get_favorite_people = lambda: []
    main_module.jellyfin_client.get_favorite_movies = lambda: [
        {
            "Name": "FAV-002 Unknown Actor Movie",
            "People": [{"Id": "person-movie-unknown-001", "Name": "Unknown Movie Actor", "Type": "Actor"}],
        }
    ]
    main_module.db.set_config("automation_mode", "suggest")

    try:
        res = await main_module.get_subscriptions()
    finally:
        main_module.jellyfin_client.reload_config = original_reload_config
        main_module.jellyfin_client.get_favorite_people = original_get_favorite_people
        main_module.jellyfin_client.get_favorite_movies = original_get_favorite_movies
        main_module.jellyfin_client.host = original_host
        main_module.jellyfin_client.api_key = original_api_key

    assert res["success"] is True
    assert res["jellyfin_sync"]["subscribed"] == 0
    assert res["jellyfin_sync"]["favorite_movie_people"]["matched"] == 1
    assert not any(row["url"] == "jellyfin:person:person-movie-unknown-001" for row in res["data"])
    assert main_module.db.get_automation_candidate(
        "favorite_actor", "jf_person:person-movie-unknown-001", "FAV-002"
    )["status"] == "candidate"


async def test_subscriptions_endpoint_syncs_favorite_movie_people_from_detail(main_module):
    original_reload_config = main_module.jellyfin_client.reload_config
    original_get_favorite_people = main_module.jellyfin_client.get_favorite_people
    original_get_favorite_movies = main_module.jellyfin_client.get_favorite_movies
    original_get_item_detail = main_module.jellyfin_client.get_item_detail
    original_host = main_module.jellyfin_client.host
    original_api_key = main_module.jellyfin_client.api_key

    main_module.jellyfin_client.reload_config = lambda: None
    main_module.jellyfin_client.host = "http://jellyfin.local"
    main_module.jellyfin_client.api_key = "scenario-key"
    main_module.jellyfin_client.get_favorite_people = lambda: []
    main_module.jellyfin_client.get_favorite_movies = lambda: [
        {
            "Id": "favorite-movie-detail-001",
            "Name": "FAV-004 Detail Movie",
        }
    ]
    main_module.jellyfin_client.get_item_detail = lambda item_id, fields="ProviderIds,People": {
        "Id": item_id,
        "People": [{"Id": "person-detail-001", "Name": "Detail Movie Actor", "Type": "Actor"}],
    }
    main_module.db.set_config("automation_mode", "suggest")

    try:
        res = await main_module.get_subscriptions()
    finally:
        main_module.jellyfin_client.reload_config = original_reload_config
        main_module.jellyfin_client.get_favorite_people = original_get_favorite_people
        main_module.jellyfin_client.get_favorite_movies = original_get_favorite_movies
        main_module.jellyfin_client.get_item_detail = original_get_item_detail
        main_module.jellyfin_client.host = original_host
        main_module.jellyfin_client.api_key = original_api_key

    assert res["success"] is True
    assert res["jellyfin_sync"]["subscribed"] == 0
    assert res["jellyfin_sync"]["favorite_movie_people"]["matched"] == 1
    assert not any(row["url"] == "jellyfin:person:person-detail-001" for row in res["data"])
    assert main_module.db.get_automation_candidate(
        "favorite_actor", "jf_person:person-detail-001", "FAV-004"
    )["status"] == "candidate"


async def test_jellyfin_image_fallback_uses_cached_movie_cover(main_module, temp_dir):
    class FakeResponse:
        def __init__(self, status_code, data=None, content_type="application/json"):
            self.status_code = status_code
            self._data = data or {}
            self.headers = {"content-type": content_type}

        def json(self):
            return self._data

        def iter_content(self, chunk_size=8192):
            return iter(())

    original_discover_user_id = main_module.jellyfin_client._discover_user_id
    original_reload_config = main_module.jellyfin_client.reload_config
    original_cache_remote_image_url = main_module.cache_remote_image_url
    original_cache_dir = main_module.COVER_CACHE_DIR
    cache_root = Path(temp_dir.name) / "fallback-cover-cache"
    cover_path = cache_root / "proxy" / "fallback-cover.jpg"
    cover_path.parent.mkdir(parents=True, exist_ok=True)
    cover_path.write_bytes(b"fake-jpg")

    main_module.COVER_CACHE_DIR = str(cache_root)
    main_module.db.set_config("jellyfin_host", "http://jellyfin.local")
    main_module.db.set_config("jellyfin_api_key", "scenario-key")
    main_module.db.save_movie({
        "id": "FAL-001",
        "title": "Fallback Movie",
        "cover": "https://example.test/fallback.jpg",
        "detail_url": "/FAL-001",
    })

    def fake_get(url, *args, **kwargs):
        if "/Images/Primary" in url:
            return FakeResponse(404, content_type="text/plain")
        if "/Users/user-001/Items/jf-fallback-001" in url:
            return FakeResponse(200, {
                "Name": "FAL-001 Fallback Movie",
                "MediaSources": [],
            })
        raise AssertionError(f"unexpected request: {url}")

    restore_transport = _stub_internal_session(main_module, get=fake_get)
    main_module.jellyfin_client.reload_config = lambda: None
    main_module.jellyfin_client._discover_user_id = lambda: "user-001"
    main_module.cache_remote_image_url = lambda url: str(cover_path)

    try:
        response = await main_module.proxy_jellyfin_image("jf-fallback-001")
    finally:
        restore_transport()
        main_module.jellyfin_client._discover_user_id = original_discover_user_id
        main_module.jellyfin_client.reload_config = original_reload_config
        main_module.cache_remote_image_url = original_cache_remote_image_url
        main_module.COVER_CACHE_DIR = original_cache_dir

    assert response.path == str(cover_path)
    assert response.headers["x-cache"] == "FALLBACK"


async def test_jellyfin_image_proxy_bypasses_invalid_cache_entry(main_module, temp_dir):
    import hashlib

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "image/jpeg"}

        def iter_content(self, chunk_size=8192):
            yield b"safe-jellyfin-image"

    original_cache_dir = main_module.COVER_CACHE_DIR

    item_id = "jf-cache-dir-001"
    cache_root = Path(temp_dir.name) / "bad-jellyfin-cache"
    cache_dir = cache_root / "jellyfin"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = f"{item_id}_360_90"
    bad_cache_entry = cache_dir / f"{hashlib.md5(cache_key.encode()).hexdigest()}.jpg"
    bad_cache_entry.mkdir()

    main_module.COVER_CACHE_DIR = str(cache_root)
    main_module.db.set_config("jellyfin_host", "http://jellyfin.local")
    main_module.db.set_config("jellyfin_api_key", "scenario-key")
    restore_transport = _stub_internal_session(
        main_module, get=lambda *args, **kwargs: FakeResponse()
    )

    try:
        response = await main_module.proxy_jellyfin_image(item_id, max_width=360, quality=90)
    finally:
        restore_transport()
        main_module.COVER_CACHE_DIR = original_cache_dir

    assert response.headers["x-cache"] == "BYPASS"
    assert response.body == b"safe-jellyfin-image"
    assert bad_cache_entry.is_dir()


async def test_jellyfin_image_proxy_rejects_oversized_stream(main_module, temp_dir):
    import requests

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "image/jpeg"}

        def iter_content(self, chunk_size=8192):
            yield b"a" * (10 * 1024 * 1024)
            yield b"b"

    original_requests_get = requests.get
    original_cache_dir = main_module.COVER_CACHE_DIR
    original_discover_user_id = main_module.jellyfin_client._discover_user_id
    original_reload_config = main_module.jellyfin_client.reload_config

    cache_root = Path(temp_dir.name) / "oversized-jellyfin-cache"
    main_module.COVER_CACHE_DIR = str(cache_root)
    main_module.db.set_config("jellyfin_host", "http://jellyfin.local")
    main_module.db.set_config("jellyfin_api_key", "scenario-key")
    requests.get = lambda *args, **kwargs: FakeResponse()
    main_module.jellyfin_client.reload_config = lambda: None
    main_module.jellyfin_client._discover_user_id = lambda: None

    try:
        try:
            await main_module.proxy_jellyfin_image("jf-oversized-001")
        except main_module.HTTPException as exc:
            assert exc.status_code == 404
        else:
            raise AssertionError("oversized Jellyfin image should be rejected")
    finally:
        requests.get = original_requests_get
        main_module.COVER_CACHE_DIR = original_cache_dir
        main_module.jellyfin_client._discover_user_id = original_discover_user_id
        main_module.jellyfin_client.reload_config = original_reload_config

    cached_files = list(cache_root.rglob("*")) if cache_root.exists() else []
    assert not [path for path in cached_files if path.is_file()]


async def test_jellyfin_image_proxy_rejects_svg_content(main_module, temp_dir):
    import requests

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "image/svg+xml; charset=utf-8"}

        def iter_content(self, chunk_size=8192):
            yield b"<svg></svg>"

    original_requests_get = requests.get
    original_cache_dir = main_module.COVER_CACHE_DIR
    original_discover_user_id = main_module.jellyfin_client._discover_user_id
    original_reload_config = main_module.jellyfin_client.reload_config

    cache_root = Path(temp_dir.name) / "svg-jellyfin-cache"
    main_module.COVER_CACHE_DIR = str(cache_root)
    main_module.db.set_config("jellyfin_host", "http://jellyfin.local")
    main_module.db.set_config("jellyfin_api_key", "scenario-key")
    requests.get = lambda *args, **kwargs: FakeResponse()
    main_module.jellyfin_client.reload_config = lambda: None
    main_module.jellyfin_client._discover_user_id = lambda: None

    try:
        try:
            await main_module.proxy_jellyfin_image("jf-svg-001")
        except main_module.HTTPException as exc:
            assert exc.status_code == 404
        else:
            raise AssertionError("SVG Jellyfin image should be rejected")
    finally:
        requests.get = original_requests_get
        main_module.COVER_CACHE_DIR = original_cache_dir
        main_module.jellyfin_client._discover_user_id = original_discover_user_id
        main_module.jellyfin_client.reload_config = original_reload_config

    cached_files = list(cache_root.rglob("*")) if cache_root.exists() else []
    assert not [path for path in cached_files if path.is_file()]


def test_remote_image_cache_rejects_svg_and_caches_safe_image(main_module, temp_dir):
    import requests
    import socket

    class FakeResponse:
        def __init__(self, content_type, chunks, content_length=None):
            self.status_code = 200
            self.headers = {"content-type": content_type}
            if content_length is not None:
                self.headers["content-length"] = content_length
            self._chunks = chunks

        def iter_content(self, chunk_size=65536):
            yield from self._chunks

    original_requests_get = requests.get
    original_getaddrinfo = socket.getaddrinfo
    original_cache_dir = main_module.COVER_CACHE_DIR
    original_proxy = main_module.db.get_config("system_proxy")
    responses = []
    request_calls = []

    def fake_get(*args, **kwargs):
        request_calls.append({"url": args[0] if args else kwargs.get("url"), "proxies": kwargs.get("proxies")})
        return responses.pop(0)

    main_module.COVER_CACHE_DIR = str(Path(temp_dir.name) / "cover-cache")
    main_module.db.set_config("system_proxy", "http://proxy.example:7890")
    requests.get = fake_get
    try:
        assert main_module.cache_remote_image_url("http://224.0.0.1/poster.jpg") is None
        assert request_calls == []

        responses.append(FakeResponse("image/svg+xml", [b"<svg></svg>"]))
        assert main_module.cache_remote_image_url("https://93.184.216.34/vector.svg") is None

        assert main_module.cache_remote_image_url("https://198.18.1.1/poster.jpg") is None

        socket.getaddrinfo = lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.48.229", 443))
        ]
        responses.append(FakeResponse("image/jpeg", [b"abc", b"def"], content_length="dirty"))
        cached = main_module.cache_remote_image_url("https://www.javbus.com/poster.jpg")
    finally:
        socket.getaddrinfo = original_getaddrinfo
        requests.get = original_requests_get
        main_module.COVER_CACHE_DIR = original_cache_dir
        main_module.db.set_config("system_proxy", original_proxy or "")

    assert cached
    assert Path(cached).suffix == ".jpg"
    assert Path(cached).read_bytes() == b"abcdef"
    assert request_calls[-1]["url"] == "https://www.javbus.com/poster.jpg"
    assert request_calls[-1]["proxies"] == {
        "http": "http://proxy.example:7890",
        "https": "http://proxy.example:7890",
    }


def test_remote_image_cache_ignores_invalid_cache_entry(main_module, temp_dir):
    import hashlib
    import requests
    import socket

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "image/jpeg"}

        def iter_content(self, chunk_size=65536):
            yield b"safe-remote-image"

    url = "https://www.javbus.com/poster-invalid-cache.jpg"
    cache_root = Path(temp_dir.name) / "bad-remote-cache"
    cache_dir = cache_root / "proxy"
    cache_dir.mkdir(parents=True, exist_ok=True)
    bad_cache_entry = cache_dir / f"{hashlib.sha256(url.encode('utf-8')).hexdigest()}.jpg"
    bad_cache_entry.mkdir()

    original_requests_get = requests.get
    original_getaddrinfo = socket.getaddrinfo
    original_cache_dir = main_module.COVER_CACHE_DIR
    calls = []

    def fake_get(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("url"))
        return FakeResponse()

    main_module.COVER_CACHE_DIR = str(cache_root)
    requests.get = fake_get
    socket.getaddrinfo = lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.48.229", 443))
    ]
    try:
        cached = main_module.cache_remote_image_url(url)
    finally:
        socket.getaddrinfo = original_getaddrinfo
        requests.get = original_requests_get
        main_module.COVER_CACHE_DIR = original_cache_dir

    assert cached is None
    assert calls == [url]
    assert bad_cache_entry.is_dir()


async def test_proxy_remote_image_rejects_svg(main_module, temp_dir):
    import requests

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "image/svg+xml"}

        def iter_content(self, chunk_size=65536):
            yield b"<svg></svg>"

        def close(self):
            pass

    original_requests_get = requests.get
    original_cache_dir = main_module.COVER_CACHE_DIR
    main_module.COVER_CACHE_DIR = str(Path(temp_dir.name) / "proxy-cache")
    requests.get = lambda *args, **kwargs: FakeResponse()
    try:
        response = await main_module.proxy_remote_image("https://93.184.216.34/vector.svg")
    finally:
        requests.get = original_requests_get
        main_module.COVER_CACHE_DIR = original_cache_dir

    # SVG 带脚本能力，不在图片白名单里：取到的字节既不缓存也不透传，只回内置占位图
    assert response.headers["x-cache"] == "FALLBACK"
    assert b"<svg></svg>" not in response.body


async def test_proxy_remote_image_rejects_private_targets_before_fetch(main_module, temp_dir):
    import requests
    import socket

    original_requests_get = requests.get
    original_getaddrinfo = socket.getaddrinfo
    original_cache_dir = main_module.COVER_CACHE_DIR
    calls = []

    def fake_get(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("url"))
        raise AssertionError("private image proxy target should not be fetched")

    main_module.COVER_CACHE_DIR = str(Path(temp_dir.name) / "private-proxy-cache")
    requests.get = fake_get
    try:
        for url in (
            "http://127.0.0.1/poster.jpg",
            "http://192.168.1.10/poster.jpg",
            "http://localhost/poster.jpg",
        ):
            response = await main_module.proxy_remote_image(url)
            assert response.headers["x-cache"] == "INVALID_FALLBACK"

        socket.getaddrinfo = lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 443))
        ]
        response = await main_module.proxy_remote_image("https://example-private.test/poster.jpg")
        assert response.headers["x-cache"] == "INVALID_FALLBACK"
    finally:
        socket.getaddrinfo = original_getaddrinfo
        requests.get = original_requests_get
        main_module.COVER_CACHE_DIR = original_cache_dir

    assert calls == []


async def test_proxy_remote_image_rejects_redirect_targets_before_fetch(main_module, temp_dir):
    import requests
    import socket

    class RedirectResponse:
        status_code = 302
        headers = {"location": "https://example-private.test/internal.jpg"}

        def iter_content(self, chunk_size=65536):
            raise AssertionError("redirect target should not be fetched")

        def close(self):
            pass

    original_requests_get = requests.get
    original_getaddrinfo = socket.getaddrinfo
    original_cache_dir = main_module.COVER_CACHE_DIR
    calls = []

    def fake_get(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("url"))
        return RedirectResponse()

    def fake_getaddrinfo(host, *args, **kwargs):
        if host == "www.javbus.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        if host == "example-private.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 443))]
        return original_getaddrinfo(host, *args, **kwargs)

    main_module.COVER_CACHE_DIR = str(Path(temp_dir.name) / "redirect-proxy-cache")
    requests.get = fake_get
    socket.getaddrinfo = fake_getaddrinfo
    try:
        response = await main_module.proxy_remote_image("https://www.javbus.com/poster.jpg")
    finally:
        socket.getaddrinfo = original_getaddrinfo
        requests.get = original_requests_get
        main_module.COVER_CACHE_DIR = original_cache_dir

    assert calls == ["https://www.javbus.com/poster.jpg"]
    assert response.headers["x-cache"] == "FALLBACK"


async def test_proxy_remote_image_ignores_invalid_cache_entry(main_module, temp_dir):
    import hashlib
    import requests

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "image/jpeg"}

        def iter_content(self, chunk_size=65536):
            yield b"safe-proxy-image"

    url = "https://93.184.216.34/poster-invalid-cache.jpg"
    cache_root = Path(temp_dir.name) / "bad-proxy-cache"
    cache_dir = cache_root / "proxy"
    cache_dir.mkdir(parents=True, exist_ok=True)
    bad_cache_entry = cache_dir / f"{hashlib.sha256(url.encode('utf-8')).hexdigest()}.jpg"
    bad_cache_entry.mkdir()

    original_requests_get = requests.get
    original_cache_dir = main_module.COVER_CACHE_DIR
    calls = []

    def fake_get(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("url"))
        return FakeResponse()

    main_module.COVER_CACHE_DIR = str(cache_root)
    requests.get = fake_get
    try:
        response = await main_module.proxy_remote_image(url)
    finally:
        requests.get = original_requests_get
        main_module.COVER_CACHE_DIR = original_cache_dir

    assert response.headers["x-cache"] == "MISS"
    assert response.body == b"safe-proxy-image"
    assert calls == [url]
    assert bad_cache_entry.is_dir()


def test_wiki_infobox_fetch_uses_scraper_proxy(main_module):
    import requests

    class FakeResponse:
        status_code = 200
        content = (
            "<table class='infobox'>"
            "<tr><th>生年月日</th><td>1999年1月2日</td></tr>"
            "<tr><th>身長</th><td>160 cm</td></tr>"
            "</table>"
        ).encode("utf-8")
        encoding = "utf-8"
        apparent_encoding = "utf-8"

    original_get = requests.get
    original_session_get = main_module.scraper.session.get
    original_proxies = main_module.scraper.proxies
    original_session_proxies = dict(main_module.scraper.session.proxies)
    proxy_config = {"http": "http://proxy.example:7890", "https": "http://proxy.example:7890"}
    calls = []

    def fail_if_module_get(*args, **kwargs):
        raise AssertionError("wiki infobox must not bypass the scraper session")

    def fake_session_get(url, *args, **kwargs):
        calls.append({"url": url, "proxies": dict(main_module.scraper.session.proxies)})
        return FakeResponse()

    requests.get = fail_if_module_get
    main_module.scraper.session.get = fake_session_get
    # reload_config 的效果：system_proxy 落在 session.proxies 上，而不是每次调用传 proxies=
    main_module.scraper.proxies = proxy_config
    main_module.scraper.session.proxies.update(proxy_config)
    try:
        info = main_module.scraper._parse_wiki_infobox("https://ja.wikipedia.org/wiki/example")
    finally:
        requests.get = original_get
        main_module.scraper.session.get = original_session_get
        main_module.scraper.proxies = original_proxies
        main_module.scraper.session.proxies.clear()
        main_module.scraper.session.proxies.update(original_session_proxies)

    assert info["birth_date"] == "1999-01-02"
    assert info["height"] == "160"
    assert calls[0]["url"] == "https://ja.wikipedia.org/wiki/example"
    assert calls[0]["proxies"] == proxy_config


def test_jellyfin_routes_are_registered_before_spa_fallback(main_module):
    from fastapi.testclient import TestClient

    main_module.db.set_config("jellyfin_host", "")
    main_module.db.set_config("jellyfin_api_key", "")
    client = TestClient(main_module.app)
    response = client.get("/api/jellyfin/stats")

    assert response.status_code == 200
    assert response.json()["success"] is True


def test_jellyfin_endpoint_errors_are_sanitized(main_module):
    from fastapi.testclient import TestClient

    secret = "secret-host://token-value"
    original_reload_config = main_module.jellyfin_client.reload_config
    original_get_server_info = main_module.jellyfin_client.get_server_info
    original_host = main_module.jellyfin_client.host
    original_api_key = main_module.jellyfin_client.api_key

    main_module.jellyfin_client.reload_config = lambda: None
    main_module.jellyfin_client.host = "http://jellyfin.local"
    main_module.jellyfin_client.api_key = "scenario-key"

    def fail_server_info():
        raise RuntimeError(f"upstream failed {secret}")

    main_module.jellyfin_client.get_server_info = fail_server_info
    client = TestClient(main_module.app)
    try:
        response = client.get("/api/jellyfin/stats")
    finally:
        main_module.jellyfin_client.reload_config = original_reload_config
        main_module.jellyfin_client.get_server_info = original_get_server_info
        main_module.jellyfin_client.host = original_host
        main_module.jellyfin_client.api_key = original_api_key

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert body["message"] == "jellyfin_stats_failed"
    assert body["error_type"] == "RuntimeError"
    assert secret not in str(body)


def test_task_status_errors_are_sanitized(main_module):
    from fastapi.testclient import TestClient

    secret = "http://nas.local:8096?api_key=very-secret-token"
    task_name = "scenario_sanitized_task_error"
    try:
        raise RuntimeError(secret)
    except RuntimeError as exc:
        main_module.task_registry.mark_error(task_name, exc, fatal=True)

    client = TestClient(main_module.app)
    response = client.get("/api/tasks/status")

    assert response.status_code == 200
    body = response.json()
    matching = [task for task in body["data"]["tasks"] if task.get("name") == task_name]
    assert matching
    last_error = matching[0]["last_error"]
    assert last_error["type"] == "RuntimeError"
    assert last_error["message"] == "RuntimeError"
    assert "traceback" not in last_error
    assert secret not in str(body)


def test_spa_fallback_does_not_serve_files_outside_static(main_module):
    from fastapi.testclient import TestClient

    assert main_module.resolve_static_file_path("../main.py") is None

    client = TestClient(main_module.app)
    response = client.get("/../main.py")

    assert response.status_code in (404, 200)
    assert b"from fastapi import" not in response.content


def _expected_startup_task_names(main_module, extra=()):
    """BUS_TERMINAL_DISABLE_STARTUP_TASKS 有意豁免的任务（见 main.py startup_event）。

    下载闭环（groom / dispatcher）与只读的翻译进度快照被豁免：前者挡住会让下完的
    种子永远投递不到 amane 库内，后者只读写本地库，没有外部副作用。
    辅助服务健康看护（health_deps_alert_loop）同理：NAS 上没有别的监控，
    它被挡住的话备份静默失败就永远没人知道。
    """
    names = {
        "groom_torrents_task",
        "download_dispatcher_task",
        "translation_progress_snapshot_loop",
        "health_deps_alert_loop",
    }
    if main_module.config.TRANSLATION_BACKFILL_ENABLED:
        names.add("translation_backfill_loop")
    if main_module.config.FEED_PRELOAD_REFRESH_ENABLED:
        names.add("feed_preload_refresh_loop")
    if main_module.config.JUNK_SWEEP_ENABLED:
        names.add("junk_sweep_loop")
    names.update(extra)
    return names


async def test_startup_disable_skips_side_effects(main_module):
    original_import = main_module.import_actress_base_if_available
    original_jable_reload = main_module.jable_scraper.reload_config
    original_discovery_reload = main_module.discovery_scraper.reload_config
    original_start_background_task = main_module.start_background_task
    calls = []

    main_module.import_actress_base_if_available = lambda: calls.append(("side_effect", "import_base"))
    main_module.jable_scraper.reload_config = lambda: calls.append(("side_effect", "jable_reload"))
    main_module.discovery_scraper.reload_config = lambda: calls.append(("side_effect", "discovery_reload"))
    main_module.start_background_task = lambda *args, **kwargs: calls.append(("task", args[0] if args else None))
    previous_disable = os.environ.get("BUS_TERMINAL_DISABLE_STARTUP_TASKS")
    os.environ["BUS_TERMINAL_DISABLE_STARTUP_TASKS"] = "true"

    try:
        await main_module.startup_event()
    finally:
        if previous_disable is None:
            os.environ.pop("BUS_TERMINAL_DISABLE_STARTUP_TASKS", None)
        else:
            os.environ["BUS_TERMINAL_DISABLE_STARTUP_TASKS"] = previous_disable
        main_module.import_actress_base_if_available = original_import
        main_module.jable_scraper.reload_config = original_jable_reload
        main_module.discovery_scraper.reload_config = original_discovery_reload
        main_module.start_background_task = original_start_background_task

    # 有外部副作用的动作（女优库导入、两个爬虫 reload 配置）必须被跳过
    assert [name for kind, name in calls if kind == "side_effect"] == []

    # 注册的任务只能是豁免清单里的那几个：爬虫/维护类任务仍然被禁掉
    registered = {name for kind, name in calls if kind == "task"}
    assert registered == _expected_startup_task_names(main_module)
    assert {"crawl_recent_movies", "auto_crawl_subscriptions", "active_image_downloader_task"}.isdisjoint(registered)


async def test_startup_preload_only_keeps_other_tasks_disabled(main_module, monkeypatch):
    calls = []
    monkeypatch.setenv("BUS_TERMINAL_DISABLE_STARTUP_TASKS", "true")
    monkeypatch.setenv("BUS_TERMINAL_PRELOAD_ON_STARTUP", "true")
    monkeypatch.setattr(
        main_module, "start_background_task",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    await main_module.startup_event()

    # 只允许豁免清单 + 预热任务本身，其余后台任务仍然被禁
    assert {args[0] for args, _ in calls} == _expected_startup_task_names(
        main_module, extra={"information_flow_preload_startup"}
    )

    preload_args, preload_kwargs = next(
        (args, kwargs) for args, kwargs in calls if args[0] == "information_flow_preload_startup"
    )
    assert preload_args[1] is main_module.preload_information_flow_cache
    assert preload_kwargs["args"] == (main_module.INFORMATION_FLOW_PRELOAD_PAGES, False)


async def test_startup_does_not_register_empty_off_work_task(main_module):
    original_import = main_module.import_actress_base_if_available
    original_jable_reload = main_module.jable_scraper.reload_config
    original_discovery_reload = main_module.discovery_scraper.reload_config
    original_start_background_task = main_module.start_background_task
    calls = []

    main_module.import_actress_base_if_available = lambda: {}
    main_module.jable_scraper.reload_config = lambda: None
    main_module.discovery_scraper.reload_config = lambda: None
    main_module.start_background_task = lambda *args, **kwargs: calls.append(args[0] if args else None)
    previous_disable = os.environ.get("BUS_TERMINAL_DISABLE_STARTUP_TASKS")
    os.environ.pop("BUS_TERMINAL_DISABLE_STARTUP_TASKS", None)

    try:
        await main_module.startup_event()
    finally:
        if previous_disable is None:
            os.environ.pop("BUS_TERMINAL_DISABLE_STARTUP_TASKS", None)
        else:
            os.environ["BUS_TERMINAL_DISABLE_STARTUP_TASKS"] = previous_disable
        main_module.import_actress_base_if_available = original_import
        main_module.jable_scraper.reload_config = original_jable_reload
        main_module.discovery_scraper.reload_config = original_discovery_reload
        main_module.start_background_task = original_start_background_task

    assert "off_work_task_loop" not in calls
    assert "jellyfin_automation_sync_task" in calls
    assert "jellyfin_cleanup_candidate_scan" in calls


async def test_user_story_end_to_end_dry_run(main_module):
    """Exercise the full story without external services or destructive actions."""
    main_module.db.set_config("automation_mode", "auto_approve_low_risk")
    main_module.db.set_config("auto_download_enabled", "true")
    main_module.db.set_config("auto_download_daily_limit", "20")
    main_module.db.set_config("disk_threshold_gb", "1")

    movie_id = "DRY-001"
    magnet = "magnet:?xt=urn:btih:dryrun001"
    main_module.db.save_movie(
        {
            "id": movie_id,
            "title": "Dry Run New Movie",
            "original_title": "Dry Run New Movie",
            "detail_url": "/DRY-001",
            "date": "2026-06-19",
            "detail": {
                "title": "Dry Run New Movie",
                "stars": [
                    {
                        "id": "actor-dry-001",
                        "name": "Dry Actor",
                        "url": "https://www.javbus.com/star/actor-dry-001",
                    }
                ],
            },
        }
    )

    permission = main_module.evaluate_download_permission(
        movie_id,
        magnet,
        "Dry Run New Movie",
        "subscription_actor_dry",
        automated=True,
    )
    assert permission["allowed"] is True

    task_id = main_module.db.add_to_queue(
        magnet=magnet,
        movie_id=movie_id,
        title="Dry Run New Movie",
        source_url="subscription_actor_dry",
    )
    main_module.db.update_download_status(task_id, "submitted")
    main_module.db.record_download(movie_id)
    main_module.db.save_local_file(
        movie_id,
        "/downloads/Dry Run New Movie.mp4",
        "/jellyfin/Dry Run New Movie/DRY-001.mp4",
        1024,
    )
    main_module.db.record_lifecycle_event(
        movie_id,
        "qb_delivered",
        title="Dry Run New Movie",
        download_status="delivered",
        jellyfin_status="indexed",
        source="dry_run",
        reason="Simulated qB completion and Jellyfin placement",
    )

    await main_module.jellyfin_webhook(
        main_module.JellyfinWebhookRequest(
            Name="DRY-001",
            NotificationType="PlaybackStopped",
            PlayedToCompletion=True,
        )
    )
    main_module.db.upsert_cleanup_candidate(
        jellyfin_id="jf-dry-001",
        movie_id=movie_id,
        title="Dry Run New Movie",
        file_path="/jellyfin/Dry Run New Movie/DRY-001.mp4",
        progress=100,
        is_favorite=False,
        reason="watched_not_favorite",
        source="dry_run_cleanup_scan",
    )
    cleanup_candidates = main_module.db.get_cleanup_candidates()
    assert any(row["movie_id"] == movie_id and row["status"] == "candidate" for row in cleanup_candidates)

    favorite = main_module.db.upsert_automation_candidate(
        candidate_type="favorite_actor",
        subject_id="actor-dry-001",
        subject_name="Dry Actor",
        source_movie_id=movie_id,
        source_title="Dry Run New Movie",
        source="dry_run_favorite",
        recommended_action="review_follow_and_subscribe",
        confidence=0.95,
        reason="favorite_movie_actor",
        payload={"actor_page_url": "https://www.javbus.com/star/actor-dry-001"},
    )
    approval = await main_module.approve_automation_candidate(
        favorite["id"],
        main_module.AutomationCandidateDecisionRequest(actor="dry-run", reason="liked actor"),
    )
    assert approval["success"] is True
    assert approval["application"]["subscription_visible"] is True
    assert main_module.db.get_actress("actor-dry-001")["is_followed"] in (1, True)
    assert any(row["url"] == "https://www.javbus.com/star/actor-dry-001" for row in main_module.db.get_subscriptions())

    next_movie_id = "DRY-002"
    next_magnet = "magnet:?xt=urn:btih:dryrun002"
    next_permission = main_module.evaluate_download_permission(
        next_movie_id,
        next_magnet,
        "Dry Actor New Release",
        "subscription_actor_dry",
        automated=True,
    )
    assert next_permission["allowed"] is True
    next_task_id = main_module.db.add_to_queue(
        magnet=next_magnet,
        movie_id=next_movie_id,
        title="Dry Actor New Release",
        source_url="subscription_actor_dry",
    )
    assert next_task_id

    lifecycle = main_module.db.get_movie_lifecycle(movie_id=movie_id)
    assert lifecycle
    assert lifecycle[0]["download_status"] == "delivered"
    assert lifecycle[0]["jellyfin_status"] == "watched"
    assert lifecycle[0]["cleanup_status"] == "candidate"

    dry_events = {event["event_type"] for event in main_module.db.get_movie_lifecycle_events(movie_id)}
    assert {
        "download_queued",
        "download_submitted",
        "download_recorded",
        "qb_delivered",
        "jellyfin_watched",
        "cleanup_candidate_detected",
    }.issubset(dry_events)

    next_events = {event["event_type"] for event in main_module.db.get_movie_lifecycle_events(next_movie_id)}
    assert "download_queued" in next_events


def test_api_state_endpoints(main_module):
    from fastapi.testclient import TestClient

    client = TestClient(main_module.app)

    live_res = client.get("/api/health/live")
    assert live_res.status_code == 200
    assert live_res.json()["status"] == "ok"

    check_res = client.get("/api/health/check")
    assert check_res.status_code == 200
    assert check_res.json()["status"] == "ok"

    lifecycle_res = client.get("/api/lifecycle/movies", params={"movie_id": "DRY-001"})
    assert lifecycle_res.status_code == 200
    lifecycle_body = lifecycle_res.json()
    assert lifecycle_body["success"] is True
    assert lifecycle_body["data"][0]["movie_id"] == "DRY-001"
    assert lifecycle_body["data"][0]["cleanup_status"] == "candidate"

    events_res = client.get("/api/lifecycle/movies/DRY-001/events")
    assert events_res.status_code == 200
    event_types = {event["event_type"] for event in events_res.json()["data"]}
    assert "download_queued" in event_types
    assert "cleanup_candidate_detected" in event_types

    cleanup_res = client.get("/api/cleanup/candidates")
    assert cleanup_res.status_code == 200
    cleanup_rows = cleanup_res.json()["data"]
    assert any(row["movie_id"] == "DRY-001" for row in cleanup_rows)

    automation_res = client.get(
        "/api/automation/candidates",
        params={"status": "approved", "candidate_type": "favorite_actor"},
    )
    assert automation_res.status_code == 200
    automation_rows = automation_res.json()["data"]
    assert any(row["subject_id"] == "actor-dry-001" for row in automation_rows)


def test_search_endpoint_limit_offset_guardrails(main_module):
    from fastapi.testclient import TestClient

    client = TestClient(main_module.app)
    for index in range(3):
        main_module.db.save_movie({
            "id": f"SEARCH-GUARD-{index}",
            "title": f"Search Guard Movie {index}",
            "original_title": f"Search Guard Original {index}",
            "detail_url": f"/SEARCH-GUARD-{index}",
            "release_date": f"2026-01-0{index + 1}",
        })

    invalid_limit = client.get("/api/search", params={"q": "Search Guard", "limit": -1})
    assert invalid_limit.status_code == 422

    too_large_limit = client.get("/api/search", params={"q": "Search Guard", "limit": 1000})
    assert too_large_limit.status_code == 422

    invalid_offset = client.get("/api/search", params={"q": "Search Guard", "offset": -1})
    assert invalid_offset.status_code == 422

    valid = client.get("/api/search", params={"q": "Search Guard", "limit": 2, "offset": 0})
    assert valid.status_code == 200
    body = valid.json()
    assert body["success"] is True
    assert body["count"] == 2


def test_discovery_query_guardrails(main_module):
    from fastapi.testclient import TestClient

    client = TestClient(main_module.app)
    calls = []
    original_jable_hot = main_module.jable_scraper.get_hot_movies
    original_jable_reload = main_module.jable_scraper.reload_config
    original_discovery_reload = main_module.discovery_scraper.reload_config

    def fake_jable_hot(*args, **kwargs):
        calls.append(kwargs)
        return [{
            "id": "DISC-001",
            "title": "Discovery Guard",
            "detail_url": "https://example.test/DISC-001",
        }]

    main_module.jable_scraper.get_hot_movies = fake_jable_hot
    main_module.jable_scraper.reload_config = lambda: None
    main_module.discovery_scraper.reload_config = lambda: None
    try:
        invalid_source = client.get("/api/discovery/movies", params={"source": "evil"})
        assert invalid_source.status_code == 400
        assert invalid_source.json()["detail"] == "unsupported_discovery_source"

        invalid_rank = client.get(
            "/api/discovery/movies/raw",
            params={"source": "jable", "rank_type": "daily&from=999"},
        )
        assert invalid_rank.status_code == 400
        assert invalid_rank.json()["detail"] == "invalid_rank_type"
        assert calls == []

        invalid_page = client.get("/api/discovery/movies", params={"source": "jable", "page": 101})
        assert invalid_page.status_code == 422
        assert calls == []

        valid = client.get(
            "/api/discovery/movies/raw",
            params={"source": "jable", "rank_type": "weekly", "page": 2},
        )
        assert valid.status_code == 200
        assert valid.json()["success"] is True
        assert valid.json()["source"] == "jable"
        assert calls[-1]["sort_by"] == "weekly"
        assert calls[-1]["page"] == 2
    finally:
        main_module.jable_scraper.get_hot_movies = original_jable_hot
        main_module.jable_scraper.reload_config = original_jable_reload
        main_module.discovery_scraper.reload_config = original_discovery_reload


def test_discovery_cache_tolerates_dirty_updated_at(main_module):
    from fastapi.testclient import TestClient

    client = TestClient(main_module.app)
    cache_key = main_module.get_discovery_cache_key("jable", 1, "daily")
    cached_movies = [{
        "id": "DIRTY-CACHE-001",
        "title": "Dirty Cache Movie",
        "detail_url": "https://example.test/DIRTY-CACHE-001",
    }]
    main_module.db.set_cache(cache_key, cached_movies)
    conn = main_module.db.get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE cache SET updated_at = ? WHERE key = ?",
            ("not-a-cache-time", cache_key),
        )
        conn.commit()
    finally:
        main_module.db._release_connection(conn)

    refresh_calls = []
    original_schedule_refresh = main_module.schedule_discovery_refresh
    main_module.schedule_discovery_refresh = lambda *args, **kwargs: refresh_calls.append((args, kwargs)) or True
    try:
        response = client.get("/api/discovery/movies", params={"source": "jable", "page": 1})
    finally:
        main_module.schedule_discovery_refresh = original_schedule_refresh

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["cached"] is True
    assert body["stale"] is True
    assert body["cache_status"] == "stale"
    assert body["cache_age_seconds"] >= 0
    assert body["data"][0]["id"] == "DIRTY-CACHE-001"
    assert refresh_calls


def test_discovery_fresh_cache_does_not_fetch_remote(main_module):
    service = main_module.get_discovery_service()
    cache_key = service.get_cache_key("jable", 1, "daily")
    main_module.db.set_cache(cache_key, [{"id": "CACHE-HIT-001", "title": "Cached"}])
    original_fetch = service.fetch_movies
    service.fetch_movies = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("fresh cache must not access the remote source")
    )
    try:
        result = service.get_cached_movies("jable", 1, "daily")
    finally:
        service.fetch_movies = original_fetch

    assert result["cache_status"] == "fresh"
    assert result["cached"] is True
    assert result["data"][0]["id"] == "CACHE-HIT-001"


def test_movie_normalization_deduplicates_and_fills_missing_fields():
    from movie_normalization import normalize_movie_list

    result = normalize_movie_list([
        {
            "id": " sgki-091 ",
            "title": "First title wins",
            "cover": "",
            "detail_url": "",
        },
        {
            "movie_id": "SGKI-091",
            "title": "Duplicate title",
            "cover": "https://example.test/sgki-091.jpg",
            "detail_url": "https://example.test/SGKI-091",
            "source": "fallback",
        },
        {
            "id": "unknown",
            "title": "URL identity",
            "detail_url": "https://example.test/item/1/",
        },
        {
            "title": "Duplicate by URL",
            "detail_url": "https://example.test/item/1",
            "duration": "120",
        },
        None,
    ])

    assert len(result) == 2
    assert result[0]["id"] == "SGKI-091"
    assert result[0]["title"] == "First title wins"
    assert result[0]["cover"] == "https://example.test/sgki-091.jpg"
    assert result[0]["detail_url"] == "https://example.test/SGKI-091"
    assert result[0]["source"] == "fallback"
    assert result[1]["id"] == ""
    assert result[1]["duration"] == "120"


def test_discovery_fresh_cache_deduplicates_without_remote_fetch(main_module):
    service = main_module.get_discovery_service()
    cache_key = service.get_cache_key("jable", 2, "daily")
    main_module.db.set_cache(cache_key, [
        {"id": "dup-001", "title": "Cached", "cover": ""},
        {"id": "DUP-001", "title": "Duplicate", "cover": "cover.jpg"},
    ])
    original_fetch = service.fetch_movies
    service.fetch_movies = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("fresh cache must not access the remote source")
    )
    try:
        result = service.get_cached_movies("jable", 2, "daily")
    finally:
        service.fetch_movies = original_fetch

    assert result["cache_status"] == "fresh"
    assert result["cached"] is True
    assert result["data"] == [{"id": "DUP-001", "title": "Cached", "cover": "cover.jpg"}]


def test_subscription_match_creates_review_candidate_when_auto_download_is_disabled(main_module):
    service = main_module.get_subscription_service()
    main_module.db.set_config("auto_download_enabled", "false")
    source_url = main_module.normalize_scraper_url("/genre/candidate-review")
    movie_id = "CANDIDATE-REVIEW-001"
    magnet = "magnet:?xt=urn:btih:" + "c" * 40
    original_get_movies = main_module.scraper.get_movies
    original_get_movie_detail = main_module.scraper.get_movie_detail
    original_select_best_magnet = main_module.scraper.select_best_magnet

    main_module.scraper.get_movies = lambda *args, **kwargs: [{
        "id": movie_id,
        "title": "Review candidate",
        "date": "2026-07-01",
        "detail_url": f"/{movie_id}",
    }]
    main_module.scraper.get_movie_detail = lambda *args, **kwargs: {
        "title": "Review candidate",
        "magnets": [{"magnet": magnet, "resolution": "1080P"}],
    }
    main_module.scraper.select_best_magnet = lambda magnets, preference="1080P": magnets[0]
    try:
        report = service.process_subscription({
            "url": source_url,
            "name": "Candidate review subscription",
            "category_type": "category",
            "start_date": "2026-01-01",
            "exclude_tags": "",
        })
    finally:
        main_module.scraper.get_movies = original_get_movies
        main_module.scraper.get_movie_detail = original_get_movie_detail
        main_module.scraper.select_best_magnet = original_select_best_magnet

    candidate = main_module.db.get_automation_candidate(
        "subscription_download",
        movie_id,
        movie_id,
    )
    decoded = main_module.get_automation_service().decode_candidate(candidate)
    assert report["added_count"] == 0
    assert report["pending_count"] == 1
    assert report["logs"][0]["reason"] == "automation_candidate_created"
    assert decoded["status"] == "candidate"
    assert decoded["source"] == "subscription"
    assert decoded["reason"] == "subscription_match_requires_approval"
    assert decoded["payload"]["matched_rules"] == ["subscription_match", "category:category"]
    assert main_module.db.get_active_queue_entry(movie_id=movie_id, magnet=magnet) is None
    lifecycle = main_module.db.get_movie_lifecycle(movie_id=movie_id)[0]
    assert lifecycle["decision_status"] == "candidate"


def test_approved_subscription_candidate_enters_local_queue_without_qb_access(main_module):
    movie_id = "CANDIDATE-APPROVE-001"
    magnet = "magnet:?xt=urn:btih:" + "d" * 40
    candidate = main_module.db.upsert_automation_candidate(
        "subscription_download",
        movie_id,
        "Approved candidate",
        movie_id,
        "Approved candidate",
        "subscription",
        "review_download",
        "subscription_match_requires_approval",
        0.7,
        {
            "magnet": magnet,
            "source_url": "https://www.javbus.com/genre/test",
            "matched_rules": ["subscription_match"],
        },
    )
    original_get_qb_session = main_module.get_qb_session
    main_module.get_qb_session = lambda: (_ for _ in ()).throw(
        AssertionError("candidate approval must not access qB")
    )
    try:
        result = main_module.get_automation_service().approve_candidate(
            candidate["id"],
            "user",
            "approved for local queue",
        )
    finally:
        main_module.get_qb_session = original_get_qb_session

    queue_row = main_module.db.get_active_queue_entry(movie_id=movie_id, magnet=magnet)
    assert result["success"] is True
    assert result["application"]["action"] == "queue_download"
    assert result["application"]["queued"] is True
    assert result["data"]["status"] == "approved"
    assert queue_row is not None
    assert queue_row["status"] == "pending"
    lifecycle = main_module.db.get_movie_lifecycle(movie_id=movie_id)[0]
    events = main_module.db.get_movie_lifecycle_events(movie_id, 20)
    assert lifecycle["decision_status"] == "approved"
    assert any(
        event["event_type"] == "download_candidate_approved"
        and event["old_status"] == "candidate"
        and event["new_status"] == "approved"
        for event in events
    )

    duplicate = main_module.get_automation_service().approve_candidate(
        candidate["id"],
        "user",
        "duplicate approval",
    )
    conflict = main_module.get_automation_service().reject_candidate(
        candidate["id"],
        "user",
        "conflicting rejection",
    )
    assert duplicate["success"] is True
    assert duplicate["duplicate"] is True
    assert conflict["success"] is False
    assert conflict["message"] == "candidate_state_conflict"
    assert main_module.db.get_movie_lifecycle(movie_id=movie_id)[0]["decision_status"] == "approved"


def test_automation_candidate_query_guardrails(main_module):
    from fastapi.testclient import TestClient

    client = TestClient(main_module.app)
    invalid_status = client.get("/api/automation/candidates", params={"status": "deleted"})
    invalid_type = client.get("/api/automation/candidates", params={"candidate_type": "shell_task"})
    all_candidates = client.get("/api/automation/candidates", params={"status": "", "candidate_type": ""})

    assert invalid_status.status_code == 422
    assert invalid_type.status_code == 422
    assert all_candidates.status_code == 200
    body = all_candidates.json()
    assert body["success"] is True
    assert body["count"] == len(body["data"])
    assert body["limit"] == 100
    assert body["offset"] == 0


def test_subscription_refresh_does_not_reopen_rejected_download_candidate(main_module):
    service = main_module.get_subscription_service()
    main_module.db.set_config("auto_download_enabled", "false")
    movie_id = "CANDIDATE-REJECTED-001"
    magnet = "magnet:?xt=urn:btih:" + "e" * 40
    candidate = main_module.db.upsert_automation_candidate(
        "subscription_download",
        movie_id,
        "Rejected candidate",
        movie_id,
        "Rejected candidate",
        "subscription",
        "review_download",
        "subscription_match_requires_approval",
        payload={"magnet": magnet},
    )
    main_module.db.update_automation_candidate_status(candidate["id"], "rejected", "user", "not wanted")
    original_get_movies = main_module.scraper.get_movies
    original_get_movie_detail = main_module.scraper.get_movie_detail
    original_select_best_magnet = main_module.scraper.select_best_magnet
    main_module.scraper.get_movies = lambda *args, **kwargs: [{
        "id": movie_id,
        "title": "Rejected candidate",
        "detail_url": f"/{movie_id}",
    }]
    main_module.scraper.get_movie_detail = lambda *args, **kwargs: {
        "magnets": [{"magnet": magnet, "resolution": "1080P"}],
    }
    main_module.scraper.select_best_magnet = lambda magnets, preference="1080P": magnets[0]
    try:
        report = service.process_subscription({
            "url": main_module.normalize_scraper_url("/genre/rejected-candidate"),
            "name": "Rejected candidate subscription",
            "category_type": "category",
        })
    finally:
        main_module.scraper.get_movies = original_get_movies
        main_module.scraper.get_movie_detail = original_get_movie_detail
        main_module.scraper.select_best_magnet = original_select_best_magnet

    refreshed = main_module.db.get_automation_candidate_by_id(candidate["id"])
    assert report["pending_count"] == 0
    assert report["skipped_count"] == 1
    assert report["logs"][0]["reason"] == "automation_candidate_previously_rejected"
    assert refreshed["status"] == "rejected"


def test_structured_cache_metrics_track_reads_writes_and_stale(main_module):
    key = "discovery_metrics_daily_p1"
    before = main_module.db.get_cache_metrics()["categories"].get("discovery_feeds", {})
    main_module.db.set_cache(key, [{"id": "METRIC-001"}])
    assert main_module.db.get_cache(key)[0]["id"] == "METRIC-001"
    assert main_module.db.get_cache("discovery_metrics_missing") is None
    main_module.db.record_cache_event(key, "stale")
    after = main_module.db.get_cache_metrics()["categories"]["discovery_feeds"]

    assert after.get("write", 0) - before.get("write", 0) == 1
    assert after.get("hit", 0) - before.get("hit", 0) == 1
    assert after.get("miss", 0) - before.get("miss", 0) == 1
    assert after.get("stale", 0) - before.get("stale", 0) == 1
    assert 0 <= after["hit_rate"] <= 1


def test_dashboard_status_uses_local_hints_without_external_probes(main_module):
    import json
    from fastapi.testclient import TestClient

    client = TestClient(main_module.app)
    original_qb_config = main_module.db.get_config("qb_config")
    original_jellyfin_host = main_module.jellyfin_client.host
    original_jellyfin_api_key = main_module.jellyfin_client.api_key
    original_get_qb_session = main_module.get_qb_session

    main_module.db.set_config("qb_config", {"host": "http://private-qb.local", "port": 8080})
    main_module.jellyfin_client.host = "http://private-jellyfin.local:8096"
    main_module.jellyfin_client.api_key = "private-jellyfin-key"
    main_module.get_qb_session = lambda: (_ for _ in ()).throw(AssertionError("qB probe called"))
    try:
        res = client.get("/api/stats/dashboard")
    finally:
        main_module.db.set_config("qb_config", original_qb_config)
        main_module.jellyfin_client.host = original_jellyfin_host
        main_module.jellyfin_client.api_key = original_jellyfin_api_key
        main_module.get_qb_session = original_get_qb_session

    assert res.status_code == 200
    body = res.json()
    data = body["data"]
    serialized = json.dumps(body)
    assert data["qb_status"]["connected"] is None
    assert data["qb_status"]["configured"] is True
    assert data["jellyfin_status"]["connected"] is None
    assert data["jellyfin_status"]["configured"] is True
    assert "url" not in data["qb_status"]
    assert "url" not in data["jellyfin_status"]
    assert "qb.local" not in serialized
    assert "private-jellyfin.local" not in serialized
    assert "private-jellyfin-key" not in serialized


def test_dashboard_reuses_fresh_metrics_snapshot(main_module):
    from fastapi.testclient import TestClient

    snapshot = {
        "movies_count": 101,
        "watched_count": 7,
        "actresses_count": 23,
        "followed_actresses_count": 5,
        "subscribed_actresses_count": 5,
        "total_downloads": 17,
        "subscriptions_count": 3,
        "queue_stats": {"pending": 2},
        "download_trend": [{"date": "2026-01-01", "count": 1}],
    }
    main_module.db.set_cache(main_module.DASHBOARD_METRICS_CACHE_KEY, snapshot)
    original_fetch = main_module.fetch_dashboard_metrics
    main_module.fetch_dashboard_metrics = lambda: (_ for _ in ()).throw(
        AssertionError("fresh dashboard cache triggered an expensive query")
    )
    try:
        response = TestClient(main_module.app).get("/api/stats/dashboard")
    finally:
        main_module.fetch_dashboard_metrics = original_fetch

    assert response.status_code == 200
    assert response.json()["data"]["movies_count"] == 101
    assert response.json()["data"]["queue_stats"] == {"pending": 2}


def test_config_secrets_are_masked_and_preserved(main_module):
    from fastapi.testclient import TestClient

    client = TestClient(main_module.app)
    main_module.db.set_config("baidu_secret", "baidu-old-secret")
    main_module.db.set_config("system_proxy", "http://proxy-user:proxy-pass@proxy.local:7890")
    main_module.db.set_config("jellyfin_host", "http://jellyfin.local:8096")
    main_module.db.set_config("jellyfin_api_key", "jellyfin-old-key")
    main_module.db.set_config("jellyfin_library_name", "Private Library")
    main_module.db.set_config("email_pass", "email-old-pass")
    main_module.db.set_config(
        "qb_config",
        {
            "host": "http://qb.local",
            "port": 8080,
            "username": "admin",
            "password": "qb-old-pass",
        },
    )

    config_res = client.get("/api/config/system")
    assert config_res.status_code == 200
    config_body = config_res.json()["data"]
    assert config_body["baidu_secret"] == ""
    assert config_body["system_proxy"] == ""
    assert config_body["jellyfin_api_key"] == ""
    assert config_body["email_pass"] == ""
    assert config_body["baidu_secret_configured"] is True
    assert config_body["system_proxy_configured"] is True
    assert config_body["jellyfin_api_key_configured"] is True
    assert config_body["email_pass_configured"] is True
    assert config_body["automation_mode"] in {"off", "suggest", "auto_approve_low_risk"}
    assert isinstance(config_body["auto_follow_enabled"], bool)
    assert isinstance(config_body["auto_subscribe_enabled"], bool)
    assert isinstance(config_body["auto_download_enabled"], bool)
    assert isinstance(config_body["auto_cleanup_enabled"], bool)

    config_body["disk_threshold_gb"] = 901
    empty_secret_res = client.post("/api/config/system", json=config_body)
    assert empty_secret_res.status_code == 200
    assert main_module.db.get_config("baidu_secret") == "baidu-old-secret"
    assert main_module.db.get_config("system_proxy") == "http://proxy-user:proxy-pass@proxy.local:7890"
    assert main_module.db.get_config("jellyfin_api_key") == "jellyfin-old-key"
    assert main_module.db.get_config("email_pass") == "email-old-pass"

    partial_config_res = client.post(
        "/api/config/system",
        json={
            "disk_threshold_gb": 902,
            "seeding_time_limit_min": 0,
        },
    )
    assert partial_config_res.status_code == 200
    assert partial_config_res.json()["success"] is True
    assert main_module.db.get_config("disk_threshold_gb") == 902
    assert main_module.db.get_config("seeding_time_limit_min") == 0
    assert main_module.db.get_config("jellyfin_host") == "http://jellyfin.local:8096"
    assert main_module.db.get_config("jellyfin_api_key") == "jellyfin-old-key"
    assert main_module.db.get_config("system_proxy") == "http://proxy-user:proxy-pass@proxy.local:7890"
    assert main_module.db.get_config("jellyfin_library_name") == "Private Library"

    host_change_without_key_res = client.post(
        "/api/config/system",
        json={"jellyfin_host": "http://jellyfin-alt.local:8096"},
    )
    assert host_change_without_key_res.status_code == 200
    host_change_without_key_body = host_change_without_key_res.json()
    assert host_change_without_key_body["success"] is False
    assert "jellyfin_api_key_required_for_host_change" in host_change_without_key_body["errors"]
    assert main_module.db.get_config("jellyfin_host") == "http://jellyfin.local:8096"
    assert main_module.db.get_config("jellyfin_api_key") == "jellyfin-old-key"

    host_change_with_key_res = client.post(
        "/api/config/system",
        json={
            "jellyfin_host": "http://jellyfin-alt.local:8096",
            "jellyfin_api_key": "jellyfin-new-key",
        },
    )
    assert host_change_with_key_res.status_code == 200
    host_change_with_key_body = host_change_with_key_res.json()
    assert host_change_with_key_body["success"] is True
    assert main_module.db.get_config("jellyfin_host") == "http://jellyfin-alt.local:8096"
    assert main_module.db.get_config("jellyfin_api_key") == "jellyfin-new-key"
    main_module.db.set_config("jellyfin_host", "http://jellyfin.local:8096")
    main_module.db.set_config("jellyfin_api_key", "jellyfin-old-key")

    automation_config_res = client.post(
        "/api/config/system",
        json={
            "automation_mode": "auto_approve_low_risk",
            "auto_follow_enabled": True,
            "auto_subscribe_enabled": False,
            "auto_download_enabled": False,
            "auto_cleanup_enabled": True,
        },
    )
    assert automation_config_res.status_code == 200
    automation_config = automation_config_res.json()["data"]
    assert automation_config["automation_mode"] == "auto_approve_low_risk"
    assert automation_config["auto_follow_enabled"] is True
    assert automation_config["auto_subscribe_enabled"] is False
    assert automation_config["auto_cleanup_enabled"] is True
    assert automation_config["jellyfin_auto_approve_candidates"] is True

    toggle_only_res = client.post(
        "/api/config/system",
        json={"jellyfin_delete_enabled": True},
    )
    assert toggle_only_res.status_code == 200
    assert toggle_only_res.json()["success"] is True
    assert main_module.db.get_config("jellyfin_delete_enabled") is True
    assert main_module.db.get_config("disk_threshold_gb") == 902
    assert main_module.db.get_config("seeding_time_limit_min") == 0
    assert main_module.db.get_config("jellyfin_host") == "http://jellyfin.local:8096"

    invalid_config = dict(config_body)
    invalid_config["disk_threshold_gb"] = -1
    invalid_config["cleanup_progress_threshold"] = 101
    invalid_config["email_port"] = 70000
    invalid_config_res = client.post("/api/config/system", json=invalid_config)
    assert invalid_config_res.status_code == 200
    invalid_config_body = invalid_config_res.json()
    assert invalid_config_body["success"] is False
    assert "disk_threshold_gb_below_min" in invalid_config_body["errors"]
    assert "cleanup_progress_threshold_above_max" in invalid_config_body["errors"]
    assert "email_port_above_max" in invalid_config_body["errors"]
    assert main_module.db.get_config("disk_threshold_gb") == 902

    invalid_url_config = dict(config_body)
    invalid_url_config["fixed_domain"] = "https://www.javbus.com/admin?token=bad"
    invalid_url_config["jellyfin_host"] = "http://user:pass@jellyfin.local:8096"
    invalid_url_config["system_proxy"] = "http://proxy.local:7890/path"
    invalid_url_config["delivery_folder"] = "../outside"
    invalid_url_config_res = client.post("/api/config/system", json=invalid_url_config)
    assert invalid_url_config_res.status_code == 200
    invalid_url_body = invalid_url_config_res.json()
    assert invalid_url_body["success"] is False
    assert "fixed_domain_invalid_url" in invalid_url_body["errors"]
    assert "jellyfin_host_invalid_url" in invalid_url_body["errors"]
    assert "system_proxy_invalid_url" in invalid_url_body["errors"]
    assert "delivery_folder_invalid_path" in invalid_url_body["errors"]
    assert main_module.db.get_config("jellyfin_host") == "http://jellyfin.local:8096"
    assert main_module.db.get_config("system_proxy") == "http://proxy-user:proxy-pass@proxy.local:7890"
    assert main_module.db.get_config("delivery_folder") != "../outside"

    valid_delivery_folder_res = client.post(
        "/api/config/system",
        json={"delivery_folder": "/downloads/staging/custom"},
    )
    assert valid_delivery_folder_res.status_code == 200
    assert valid_delivery_folder_res.json()["success"] is True
    assert main_module.db.get_config("delivery_folder") == "/downloads/staging/custom"
    assert main_module.normalize_qb_save_path("/downloads/staging/custom/manual") == "/downloads/staging/custom/manual"


    main_module.db.set_config("delivery_folder", "../outside")
    assert main_module.get_qb_delivery_folder() is None
    main_module.db.set_config("delivery_folder", "/downloads/staging/custom")

    config_body["baidu_secret"] = "baidu-new-secret"
    new_secret_res = client.post("/api/config/system", json=config_body)
    assert new_secret_res.status_code == 200
    assert main_module.db.get_config("baidu_secret") == "baidu-new-secret"
    assert new_secret_res.json()["data"]["baidu_secret"] == ""

    qb_res = client.get("/api/qb/config")
    assert qb_res.status_code == 200
    assert qb_res.json()["data"]["password"] == ""
    assert qb_res.json()["data"]["password_configured"] is True

    main_module.db.set_config(
        "qb_config",
        {
            "host": "http://qb.local",
            "port": "not-a-port",
            "username": "admin",
            "password": "qb-old-pass",
        },
    )
    dirty_port_qb_res = client.get("/api/qb/config")
    assert dirty_port_qb_res.status_code == 200
    assert dirty_port_qb_res.json()["data"]["port"] == 8080
    assert dirty_port_qb_res.json()["data"]["password"] == ""
    assert dirty_port_qb_res.json()["data"]["password_configured"] is True
    main_module.db.set_config(
        "qb_config",
        {
            "host": "http://qb.local",
            "port": 8080,
            "username": "admin",
            "password": "qb-old-pass",
        },
    )

    keep_password_res = client.post(
        "/api/qb/config",
        json={
            "host": "http://qb.local",
            "port": 8080,
            "username": "admin",
            "password": "",
        },
    )
    assert keep_password_res.status_code == 200
    assert keep_password_res.json()["data"]["password"] == ""
    assert keep_password_res.json()["data"]["password_configured"] is True
    assert main_module.db.get_config("qb_config")["password"] == "qb-old-pass"

    invalid_port_res = client.post(
        "/api/qb/config",
        json={
            "host": "http://qb.local",
            "port": 70000,
            "username": "admin",
            "password": "",
        },
    )
    assert invalid_port_res.status_code == 200
    assert invalid_port_res.json()["success"] is False
    assert invalid_port_res.json()["message"] == "invalid_qb_port"

    invalid_host_res = client.post(
        "/api/qb/config",
        json={
            "host": "http://qb.local/path",
            "port": 8080,
            "username": "admin",
            "password": "",
        },
    )
    assert invalid_host_res.status_code == 200
    assert invalid_host_res.json()["success"] is False
    assert invalid_host_res.json()["message"] == "invalid_qb_host"

    # 行为变更（v1.65）：host 里带端口现在**合法**，端口优先于 port 字段。
    # 原来这里断言 invalid_qb_host，是为了防base_url 拼成双端口；
    # 现在要验的是：接受它，且结果里端口被正确拆分、没有双端口。
    host_with_url_port_res = client.post(
        "/api/qb/config",
        json={
            "host": "http://qb.local:9090",
            "port": 8080,
            "username": "admin",
            "password": "",
        },
    )
    assert host_with_url_port_res.status_code == 200
    assert host_with_url_port_res.json()["success"] is True
    saved_qb = host_with_url_port_res.json()["data"]
    assert saved_qb["host"] == "http://qb.local", f"host 未剥离端口: {saved_qb}"
    assert saved_qb["port"] == 9090, f"应采用地址里的端口 9090，实际 {saved_qb.get('port')}"

    # 带路径仍然必须被拒（路径会被拼进 base_url 导致请求错误）
    host_with_path_res = client.post(
        "/api/qb/config",
        json={
            "host": "http://qb.local/webui",
            "port": 8080,
            "username": "admin",
            "password": "",
        },
    )
    assert host_with_path_res.json()["success"] is False
    assert host_with_path_res.json()["message"] == "invalid_qb_host"

    # 非数字端口必须被拒，且错误码要指向端口（invalid_qb_port）而不是地址
    bad_port_res = client.post(
        "/api/qb/config",
        json={
            "host": "http://qb.local:notaport",
            "port": 8080,
            "username": "admin",
            "password": "",
        },
    )
    assert bad_port_res.json()["success"] is False
    assert bad_port_res.json()["message"] == "invalid_qb_port"

    # 裸主机带端口（没有 http:// 前缀）同样合法
    host_with_raw_port_res = client.post(
        "/api/qb/config",
        json={
            "host": "qb.local:9090",
            "port": 8080,
            "username": "admin",
            "password": "",
        },
    )
    assert host_with_raw_port_res.status_code == 200
    assert host_with_raw_port_res.json()["success"] is True
    raw_saved = host_with_raw_port_res.json()["data"]
    assert raw_saved["host"] == "qb.local", f"裸主机未剥离端口: {raw_saved}"
    assert raw_saved["port"] == 9090, f"应采用地址里的端口 9090，实际 {raw_saved.get('port')}"

    missing_user_res = client.post(
        "/api/qb/config",
        json={
            "host": "http://qb.local",
            "port": 8080,
            "username": "   ",
            "password": "",
        },
    )
    assert missing_user_res.status_code == 200
    assert missing_user_res.json()["success"] is False
    assert missing_user_res.json()["message"] == "missing_qb_username"

    keep_blank_password_res = client.post(
        "/api/qb/config",
        json={
            "host": "http://qb.local",
            "port": 8080,
            "username": "admin",
            "password": "   ",
        },
    )
    assert keep_blank_password_res.status_code == 200
    assert main_module.db.get_config("qb_config")["password"] == "qb-old-pass"

    change_password_res = client.post(
        "/api/qb/config",
        json={
            "host": "http://qb.local",
            "port": 8080,
            "username": "admin",
            "password": "qb-new-pass",
        },
    )
    assert change_password_res.status_code == 200
    assert main_module.db.get_config("qb_config")["password"] == "qb-new-pass"
    assert change_password_res.json()["data"]["password"] == ""
    assert change_password_res.json()["data"]["password_configured"] is True

    original_qb_password = main_module.config.QB_PASSWORD
    try:
        main_module.config.QB_PASSWORD = "qb-env-pass"
        main_module.db.set_config(
            "qb_config",
            {
                "host": "http://qb.local",
                "port": 8080,
                "username": "admin",
                "password": "",
            },
        )
        env_password_res = client.get("/api/qb/config")
        assert env_password_res.status_code == 200
        assert env_password_res.json()["data"]["password"] == ""
        assert env_password_res.json()["data"]["password_configured"] is True

        keep_env_password_res = client.post(
            "/api/qb/config",
            json={
                "host": "http://qb.local",
                "port": 8080,
                "username": "admin",
                "password": "",
            },
        )
        assert keep_env_password_res.status_code == 200
        assert keep_env_password_res.json()["data"]["password"] == ""
        assert keep_env_password_res.json()["data"]["password_configured"] is True
        assert main_module.db.get_config("qb_config")["password"] == "qb-env-pass"
    finally:
        main_module.config.QB_PASSWORD = original_qb_password

    original_normalize_qb_config_payload = main_module.normalize_qb_config_payload

    def raise_sensitive_qb_error(*args, **kwargs):
        raise ValueError("invalid qb password=secret api_key=leak")

    main_module.normalize_qb_config_payload = raise_sensitive_qb_error
    try:
        sensitive_error_res = client.post(
            "/api/qb/config",
            json={
                "host": "http://qb.local",
                "port": 8080,
                "username": "admin",
                "password": "",
            },
        )
    finally:
        main_module.normalize_qb_config_payload = original_normalize_qb_config_payload

    assert sensitive_error_res.status_code == 200
    sensitive_body = sensitive_error_res.json()
    assert sensitive_body["success"] is False
    assert sensitive_body["message"] == "invalid_qb_config"
    assert "secret" not in str(sensitive_body)
    assert "leak" not in str(sensitive_body)


def test_sensitive_config_values_are_encrypted_at_rest(main_module, monkeypatch):
    monkeypatch.setenv("BUS_TERMINAL_CONFIG_SECRET", "unit-test-config-secret")
    original_key = main_module.db.get_config("jellyfin_api_key")
    test_key = "roundtrip-secret-key"

    def _raw_config_value(db, key):
        conn = db.get_connection()
        try:
            cursor = conn.cursor()
            sql = "SELECT value FROM config WHERE key = %s" if db.is_postgres else "SELECT value FROM config WHERE key = ?"
            cursor.execute(sql, (key,))
            row = cursor.fetchone()
            return row["value"] if row else None
        finally:
            db._release_connection(conn)

    try:
        main_module.db.set_config("jellyfin_api_key", test_key)
        raw_value = _raw_config_value(main_module.db, "jellyfin_api_key")
        assert raw_value is not None
        assert test_key not in raw_value
        assert "enc:v1:" in raw_value
        assert main_module.db.get_config("jellyfin_api_key") == test_key
    finally:
        if original_key:
            main_module.db.set_config("jellyfin_api_key", original_key)
        else:
            conn = main_module.db.get_connection()
            try:
                cursor = conn.cursor()
                sql = "DELETE FROM config WHERE key = %s" if main_module.db.is_postgres else "DELETE FROM config WHERE key = ?"
                cursor.execute(sql, ("jellyfin_api_key",))
                conn.commit()
            finally:
                main_module.db._release_connection(conn)

def test_system_config_default_jellyfin_library_name_is_readable(main_module):
    import json
    from fastapi.testclient import TestClient

    client = TestClient(main_module.app)
    main_module.db.set_config("jellyfin_library_name", None)

    response = client.get("/api/config/system")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["jellyfin_library_name"] == "黄片"
    assert "뼝튬" not in json.dumps(body, ensure_ascii=False)


def test_jellyfin_stats_does_not_expose_connection_details(main_module):
    import json
    from fastapi.testclient import TestClient

    client = TestClient(main_module.app)

    main_module.db.set_config("jellyfin_host", "http://private-jellyfin.local:8096")
    main_module.db.set_config("jellyfin_api_key", "")
    unconfigured_res = client.get("/api/jellyfin/stats")
    assert unconfigured_res.status_code == 200
    unconfigured_data = unconfigured_res.json()["data"]
    assert unconfigured_data["connected"] is False
    assert unconfigured_data["host_configured"] is True
    assert "host" not in unconfigured_data
    assert "private-jellyfin" not in json.dumps(unconfigured_data)

    original_get_server_info = main_module.jellyfin_client.get_server_info
    original_get_libraries = main_module.jellyfin_client.get_libraries
    original_get_library_items = main_module.jellyfin_client.get_library_items
    main_module.db.set_config("jellyfin_api_key", "private-jellyfin-key")

    main_module.jellyfin_client.get_server_info = lambda: {
        "ServerName": "Scenario Jellyfin",
        "Version": "10.9.0",
    }
    main_module.jellyfin_client.get_libraries = lambda: [
        {"id": "lib-001", "name": "Movies", "type": "movies"}
    ]
    main_module.jellyfin_client.get_library_items = lambda *args, **kwargs: {"items": [], "total": 0}

    try:
        configured_res = client.get("/api/jellyfin/stats")
    finally:
        main_module.jellyfin_client.get_server_info = original_get_server_info
        main_module.jellyfin_client.get_libraries = original_get_libraries
        main_module.jellyfin_client.get_library_items = original_get_library_items

    assert configured_res.status_code == 200
    configured_body = configured_res.json()
    configured_data = configured_body["data"]
    serialized = json.dumps(configured_body)
    assert configured_data["connected"] is True
    assert configured_data["host_configured"] is True
    assert "host" not in configured_data
    assert "private-jellyfin.local" not in serialized
    assert "private-jellyfin-key" not in serialized


def test_jellyfin_stats_tolerates_dirty_numeric_fields(main_module):
    from fastapi.testclient import TestClient

    client = TestClient(main_module.app)
    original_values = {
        key: main_module.db.get_config(key)
        for key in (
            "jellyfin_host",
            "jellyfin_api_key",
            "jellyfin_last_sync_time",
            "last_jellyfin_sync_time",
            "last_jellyfin_sync",
        )
    }
    original_get_server_info = main_module.jellyfin_client.get_server_info
    original_get_libraries = main_module.jellyfin_client.get_libraries
    original_get_library_items = main_module.jellyfin_client.get_library_items

    main_module.db.set_config("jellyfin_host", "http://jellyfin.local:8096")
    main_module.db.set_config("jellyfin_api_key", "jellyfin-key")
    main_module.db.set_config("jellyfin_last_sync_time", "not-a-time")
    main_module.db.set_config("last_jellyfin_sync_time", "")
    main_module.db.set_config("last_jellyfin_sync", "")
    main_module.jellyfin_client.get_server_info = lambda: {"ServerName": "Dirty Stats"}
    main_module.jellyfin_client.get_libraries = lambda: [{"id": "lib-dirty", "name": "Dirty Lib"}]
    main_module.jellyfin_client.get_library_items = lambda *args, **kwargs: {"items": [], "total": "not-a-count"}

    try:
        response = client.get("/api/jellyfin/stats")
    finally:
        for key, value in original_values.items():
            main_module.db.set_config(key, value if value is not None else "")
        main_module.jellyfin_client.get_server_info = original_get_server_info
        main_module.jellyfin_client.get_libraries = original_get_libraries
        main_module.jellyfin_client.get_library_items = original_get_library_items

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    data = body["data"]
    assert data["libraries"][0]["item_count"] == 0
    assert data["favorites_count"] == 0
    assert data["watched_count"] == 0
    assert data["last_sync_time"] is None


def test_jellyfin_read_only_routes_short_circuit_when_service_is_offline(main_module):
    import json
    from fastapi.testclient import TestClient

    original_host = main_module.db.get_config("jellyfin_host")
    original_key = main_module.db.get_config("jellyfin_api_key")
    original_get_server_info = main_module.jellyfin_client.get_server_info
    original_get_libraries = main_module.jellyfin_client.get_libraries
    original_get_library_items = main_module.jellyfin_client.get_library_items
    calls = {"probe": 0, "libraries": 0, "items": 0}

    def offline_probe():
        calls["probe"] += 1
        return None

    def unexpected_libraries():
        calls["libraries"] += 1
        return []

    def unexpected_items(*_args, **_kwargs):
        calls["items"] += 1
        return {"items": [], "total": 0}

    main_module.db.set_config("jellyfin_host", "http://offline-jellyfin.local:8096")
    main_module.db.set_config("jellyfin_api_key", "offline-secret-key")
    main_module.jellyfin_client.get_server_info = offline_probe
    main_module.jellyfin_client.get_libraries = unexpected_libraries
    main_module.jellyfin_client.get_library_items = unexpected_items
    service = main_module.get_jellyfin_service()
    service.clear_availability_cache()
    client = TestClient(main_module.app)
    try:
        stats = client.get("/api/jellyfin/stats")
        libraries = client.get("/api/jellyfin/libraries")
        library = client.get("/api/jellyfin/library", params={"library_id": "lib-offline"})
        behavior = client.get("/api/jellyfin/behavior/insights")
    finally:
        main_module.db.set_config("jellyfin_host", original_host or "")
        main_module.db.set_config("jellyfin_api_key", original_key or "")
        main_module.jellyfin_client.get_server_info = original_get_server_info
        main_module.jellyfin_client.get_libraries = original_get_libraries
        main_module.jellyfin_client.get_library_items = original_get_library_items
        service.clear_availability_cache()

    assert stats.json()["success"] is True
    assert stats.json()["data"]["connected"] is False
    assert stats.json()["data"]["availability"] == "offline"
    assert libraries.json()["success"] is False
    assert libraries.json()["message"] == "jellyfin_libraries_failed"
    assert library.json()["success"] is False
    assert library.json()["message"] == "jellyfin_library_query_failed"
    assert behavior.json()["success"] is False
    assert behavior.json()["message"] == "jellyfin_behavior_insights_failed"
    assert calls == {"probe": 1, "libraries": 0, "items": 0}
    serialized = json.dumps(
        [stats.json(), libraries.json(), library.json(), behavior.json()],
        ensure_ascii=False,
    )
    assert "offline-jellyfin.local" not in serialized
    assert "offline-secret-key" not in serialized


def test_jellyfin_library_query_params_are_validated(main_module):
    from fastapi.testclient import TestClient

    client = TestClient(main_module.app)
    original_reload_config = main_module.jellyfin_client.reload_config
    original_get_server_info = main_module.jellyfin_client.get_server_info
    original_get_library_items = main_module.jellyfin_client.get_library_items
    original_client_host = main_module.jellyfin_client.host
    original_client_key = main_module.jellyfin_client.api_key
    calls = []

    def fake_get_library_items(*args, **kwargs):
        calls.append(args)
        return {"items": [], "total": 0}

    main_module.jellyfin_client.reload_config = lambda: None
    main_module.jellyfin_client.host = "http://query-jellyfin.local"
    main_module.jellyfin_client.api_key = "query-key"
    main_module.jellyfin_client.get_server_info = lambda: {"ServerName": "Query Test"}
    main_module.jellyfin_client.get_library_items = fake_get_library_items
    main_module.get_jellyfin_service().clear_availability_cache()
    try:
        invalid_sort = client.get("/api/jellyfin/library", params={"sort_by": "BadField"})
        assert invalid_sort.status_code == 400
        assert invalid_sort.json()["detail"] == "invalid_sort_by"
        assert calls == []

        invalid_order = client.get("/api/jellyfin/library", params={"sort_order": "Sideways"})
        assert invalid_order.status_code == 400
        assert invalid_order.json()["detail"] == "invalid_sort_order"
        assert calls == []

        invalid_library = client.get("/api/jellyfin/library", params={"library_id": "../secret"})
        assert invalid_library.status_code == 400
        assert invalid_library.json()["detail"] == "invalid_library_id"
        assert calls == []

        too_large = client.get("/api/jellyfin/library", params={"limit": 5000})
        assert too_large.status_code == 422
        assert calls == []

        valid = client.get(
            "/api/jellyfin/library",
            params={
                "library_id": "lib-001",
                "sort_by": "playcount",
                "sort_order": "ascending",
                "limit": 200,
                "offset": 20,
            },
        )
    finally:
        main_module.jellyfin_client.reload_config = original_reload_config
        main_module.jellyfin_client.get_server_info = original_get_server_info
        main_module.jellyfin_client.get_library_items = original_get_library_items
        main_module.jellyfin_client.host = original_client_host
        main_module.jellyfin_client.api_key = original_client_key
        main_module.get_jellyfin_service().clear_availability_cache()

    assert valid.status_code == 200
    assert calls == [("lib-001", None, None, "PlayCount", "Ascending", None, 200, 20)]


def test_jellyfin_behavior_bucket_error_is_sanitized(main_module):
    from fastapi.testclient import TestClient

    client = TestClient(main_module.app)
    invalid_bucket = "bad-bucket?api_key=secret-token"
    response = client.get("/api/jellyfin/behavior/movies", params={"bucket": invalid_bucket})

    assert response.status_code == 400
    body = response.json()
    assert body["detail"] == "invalid_behavior_bucket"
    assert invalid_bucket not in str(body)
    assert "secret-token" not in str(body)


def test_jellyfin_behavior_helpers_parse_dirty_boolean_and_counts(main_module):
    dirty_movie = {
        "jellyfin_id": "dirty-behavior-001",
        "id": "DIRTY-BEHAVIOR-001",
        "title": "Dirty Behavior",
        "is_watched": "false",
        "is_favorite": "true",
        "play_count": "3",
    }

    assert main_module.matches_behavior_bucket(dirty_movie, "favorite_unwatched", 2) is True
    assert main_module.matches_behavior_bucket(dirty_movie, "watched_and_favorite", 2) is False
    assert main_module.matches_behavior_bucket(dirty_movie, "strong_preference", 2) is True

    view = main_module.behavior_movie_view(dirty_movie, "strong_preference", 2)
    assert view["is_watched"] is False
    assert view["is_favorite"] is True
    assert view["play_count"] == 3

    bad_count_movie = dict(dirty_movie, play_count="dirty")
    assert main_module.matches_behavior_bucket(bad_count_movie, "strong_preference", 2) is False
    assert main_module.behavior_movie_view(bad_count_movie, "strong_preference", 2)["play_count"] == 0


def test_subscription_stats_aggregates_counts_and_caps_recent_tasks(main_module):
    from fastapi.testclient import TestClient

    client = TestClient(main_module.app)
    source_url = "jellyfin:actress:stats-capped-001"
    response = None
    try:
        for index in range(25):
            task_id = main_module.db.add_to_queue(
                magnet=f"magnet:?xt=urn:btih:statscap{index:04d}",
                movie_id=f"STATCAP-{index:03d}",
                title=f"Stats Cap {index:03d}",
                source_url=source_url,
            )
            main_module.db.update_download_status(task_id, "submitted")

        response = client.get("/api/subscriptions/stats")
    finally:
        conn = main_module.db.get_connection()
        try:
            cursor = conn.cursor()
            placeholder = "%s" if main_module.db.is_postgres else "?"
            cursor.execute(f"DELETE FROM download_queue WHERE source_url = {placeholder}", (source_url,))
            conn.commit()
        finally:
            main_module.db._release_connection(conn)

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    stats = body["data"]
    assert stats[source_url]["count"] == 25
    assert len(stats[source_url]["tasks"]) == 15


def test_subscription_stats_aliases_internal_jellyfin_sources(main_module):
    from fastapi.testclient import TestClient

    client = TestClient(main_module.app)
    legacy_source_url = "jf_person:Stats Alias Person"
    canonical_source_url = "jellyfin:person:Stats Alias Person"
    response = None
    try:
        for index in range(2):
            task_id = main_module.db.add_to_queue(
                magnet=f"magnet:?xt=urn:btih:statsalias{index:04d}",
                movie_id=f"STATALIAS-{index:03d}",
                title=f"Stats Alias {index:03d}",
                source_url=legacy_source_url,
            )
            main_module.db.update_download_status(task_id, "submitted")

        response = client.get("/api/subscriptions/stats")
    finally:
        conn = main_module.db.get_connection()
        try:
            cursor = conn.cursor()
            placeholder = "%s" if main_module.db.is_postgres else "?"
            cursor.execute(f"DELETE FROM download_queue WHERE source_url = {placeholder}", (legacy_source_url,))
            conn.commit()
        finally:
            main_module.db._release_connection(conn)

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    stats = body["data"]
    assert stats[legacy_source_url]["count"] == 2
    assert stats[canonical_source_url]["count"] == 2
    assert len(stats[legacy_source_url]["tasks"]) == 2
    assert len(stats[canonical_source_url]["tasks"]) == 2


def test_subscription_stats_batches_decorative_cover_queries(main_module):
    service = main_module.get_subscription_stats_service()
    original_get_subscriptions = main_module.db.get_subscriptions
    original_get_covers = service.get_local_library_covers_for_terms
    calls = []
    main_module.db.get_subscriptions = lambda: [
        {"url": "jellyfin:actress:Batch One", "name": "Batch One"},
        {"url": "jellyfin:actress:Batch Two", "name": "Batch Two"},
    ]

    def fake_get_covers(actress_ids, actress_names, limit=15):
        calls.append((actress_ids, actress_names, limit))
        return {
            ("name", "Batch One"): [{"id": "ONE", "cover": "one.jpg"}],
            ("name", "Batch Two"): [{"id": "TWO", "cover": "two.jpg"}],
        }

    service.get_local_library_covers_for_terms = fake_get_covers
    stats = {}
    try:
        service._merge_decorative_covers(stats)
    finally:
        main_module.db.get_subscriptions = original_get_subscriptions
        service.get_local_library_covers_for_terms = original_get_covers

    assert len(calls) == 1
    assert stats["jellyfin:actress:Batch One"]["decorative_covers"][0]["id"] == "ONE"
    assert stats["jellyfin:actress:Batch Two"]["decorative_covers"][0]["id"] == "TWO"


def test_batch_translate_guardrails(main_module):
    from fastapi.testclient import TestClient

    client = TestClient(main_module.app)
    original_translate = main_module.scraper.batch_translate_to_chinese
    calls = []

    def fake_translate(texts):
        calls.append(list(texts))
        return [f"zh-{text}" for text in texts]

    main_module.scraper.batch_translate_to_chinese = fake_translate
    try:
        invalid_type = client.post("/api/translate/batch", json={"texts": "not-list"})
        assert invalid_type.status_code == 200
        assert invalid_type.json()["success"] is False
        assert invalid_type.json()["message"] == "texts_must_be_list"
        assert calls == []

        too_many = client.post(
            "/api/translate/batch",
            json={"texts": [f"text-{index}" for index in range(main_module.TRANSLATION_BATCH_LIMIT + 1)]},
        )
        assert too_many.status_code == 200
        assert too_many.json()["success"] is False
        assert too_many.json()["message"] == "translation_batch_too_large"
        assert calls == []

        too_long_text = "x" * (main_module.TRANSLATION_TEXT_MAX_LENGTH + 1)
        too_long = client.post("/api/translate/batch", json={"texts": [too_long_text]})
        assert too_long.status_code == 200
        assert too_long.json()["success"] is False
        assert too_long.json()["message"] == "translation_text_too_long"
        assert too_long_text not in str(too_long.json())
        assert calls == []

        valid = client.post("/api/translate/batch", json={"texts": ["Alice", "Alice", "Bob", ""]})
    finally:
        main_module.scraper.batch_translate_to_chinese = original_translate

    assert valid.status_code == 200
    assert valid.json()["success"] is True
    assert valid.json()["data"] == {"Alice": "zh-Alice", "Bob": "zh-Bob"}
    assert calls == [["Alice", "Bob"]]


def test_user_password_helpers_use_strong_hash(main_module):
    import hashlib
    from security import hash_password, verify_password

    password_hash = hash_password("initial-password")
    assert password_hash.startswith("pbkdf2_sha256$")
    assert verify_password("initial-password", password_hash) is True
    assert verify_password("wrong-password", password_hash) is False

    legacy_hash = hashlib.sha256("legacy-password".encode()).hexdigest()
    assert verify_password("legacy-password", legacy_hash) is True

    username = "scenario-admin"
    assert main_module.db.create_user(username, password_hash) is True
    assert main_module.db.create_user(username, hash_password("duplicate")) is False

    updated_hash = hash_password("updated-password")
    assert main_module.db.update_user_password(username, updated_hash) is True
    assert main_module.db.update_user_password("missing-user", updated_hash) is False

    conn = main_module.db.get_connection()
    try:
        cursor = conn.cursor()
        placeholder = "%s" if main_module.db.is_postgres else "?"
        cursor.execute(f"SELECT password_hash FROM users WHERE username = {placeholder}", (username,))
        row = cursor.fetchone()
        stored = row["password_hash"]
    finally:
        main_module.db._release_connection(conn)

    assert verify_password("updated-password", stored) is True


def test_api_logs_redact_sensitive_values(main_module):
    from fastapi.testclient import TestClient

    raw_message = (
        "password=plain-pass api_key=plain-key "
        "Authorization: Bearer plain-token "
        "X-Emby-Token: jellyfin-token "
        "system_proxy=http://proxy-user:proxy-pass@proxy.local:7890 "
        "standalone Bearer standalone-token "
        '{"password": "space pass value"} '
        "https://example.test/path?token=query-token&ok=1 "
        r"C:\private\nas\movie\SECRET-001.mp4 "
        "/mnt/private/downloads/SECRET-002.mp4"
    )

    redacted = main_module.redact_log_message(raw_message)
    for secret in (
        "plain-pass",
        "plain-key",
        "plain-token",
        "jellyfin-token",
        "proxy-user:proxy-pass",
        "standalone-token",
        "space pass value",
        "query-token",
        r"C:\private\nas\movie\SECRET-001.mp4",
        "/mnt/private/downloads/SECRET-002.mp4",
    ):
        assert secret not in redacted
    assert "password=***" in redacted
    assert "api_key=***" in redacted
    assert "system_proxy=***" in redacted
    assert "Authorization: ***" in redacted
    assert "Bearer ***" in redacted
    assert "X-Emby-Token: ***" in redacted
    assert "token=***" in redacted

    main_module.log_buffer.clear()
    main_module.log_buffer.append({
        "time": "00:00:00",
        "level": "INFO",
        "msg": raw_message,
    })

    client = TestClient(main_module.app)
    res = client.get("/api/logs")
    assert res.status_code == 200
    body = res.json()
    assert len(body) == 1
    returned = body[0]["msg"]
    for secret in (
        "plain-pass",
        "plain-key",
        "plain-token",
        "jellyfin-token",
        "proxy-user:proxy-pass",
        "standalone-token",
        "space pass value",
        "query-token",
        r"C:\private\nas\movie\SECRET-001.mp4",
        "/mnt/private/downloads/SECRET-002.mp4",
    ):
        assert secret not in returned
    assert "password=***" in returned
    assert "api_key=***" in returned
    assert "system_proxy=***" in returned


def test_organizer_path_logs_are_redacted_by_default(main_module):
    original_flag = os.environ.get("BUS_TERMINAL_SHOW_UNSAFE_PATHS")
    os.environ.pop("BUS_TERMINAL_SHOW_UNSAFE_PATHS", None)
    try:
        raw_path = r"C:\private\nas\movie\SECRET-001.mp4"
        redacted = main_module.organizer.safe_path_for_log(raw_path)
    finally:
        if original_flag is None:
            os.environ.pop("BUS_TERMINAL_SHOW_UNSAFE_PATHS", None)
        else:
            os.environ["BUS_TERMINAL_SHOW_UNSAFE_PATHS"] = original_flag

    assert raw_path not in redacted
    assert redacted.startswith("<path len=")
    assert "sha256=" in redacted


def test_organizer_rejects_library_path_escape(main_module, temp_dir):
    library_root = Path(temp_dir.name) / "jellyfin_library"
    organizer = main_module.organizer.Organizer(allow_file_writes=True)
    organizer.library_root = str(library_root)

    try:
        organizer._safe_library_path("..", "outside")
    except ValueError as exc:
        assert "outside_library_root" in str(exc) or "path_outside_library_root" in str(exc)
    else:
        raise AssertionError("expected library path escape to be rejected")


def test_organizer_sanitizes_movie_target_paths(main_module, temp_dir):
    library_root = Path(temp_dir.name) / "jellyfin_library"
    outside_root = Path(temp_dir.name) / "outside"
    source_file = Path(temp_dir.name) / "source.mp4"
    source_file.write_bytes(b"movie")

    organizer = main_module.organizer.Organizer(allow_file_writes=True)
    organizer.library_root = str(library_root)

    original_get_movie = main_module.organizer.db.get_movie
    original_save_local_file = main_module.organizer.db.save_local_file
    original_managed_roots = os.environ.get("BUS_TERMINAL_MANAGED_MEDIA_ROOTS")
    os.environ["BUS_TERMINAL_MANAGED_MEDIA_ROOTS"] = temp_dir.name
    saved_rows = []

    def fake_get_movie(movie_id):
        return {
            "id": movie_id,
            "title": r"..\..\outside\title",
            "release_date": "2026-01-01",
            "detail_json": {
                "actresses": [{"name": r"..\..\outside\actor"}],
                "genres": [],
            },
        }

    main_module.organizer.db.get_movie = fake_get_movie
    main_module.organizer.db.save_local_file = lambda *args: saved_rows.append(args) or True
    try:
        ok = organizer.create_symlink_for_movie(r"..\..\outside\movie", str(source_file))
    finally:
        main_module.organizer.db.get_movie = original_get_movie
        main_module.organizer.db.save_local_file = original_save_local_file
        if original_managed_roots is None:
            os.environ.pop("BUS_TERMINAL_MANAGED_MEDIA_ROOTS", None)
        else:
            os.environ["BUS_TERMINAL_MANAGED_MEDIA_ROOTS"] = original_managed_roots

    assert ok is True
    assert saved_rows
    symlink_path = Path(saved_rows[0][2]).resolve()
    assert symlink_path.is_relative_to(library_root.resolve())
    assert not outside_root.exists()
    assert not any(part == ".." for part in symlink_path.parts)


def test_organizer_actor_thumb_download_blocks_local_urls(main_module, temp_dir):
    import requests

    class FakeResponse:
        status_code = 200

        def __init__(self, content_type="image/jpeg"):
            self.headers = {"Content-Type": content_type} if content_type is not None else {}

        def iter_content(self, chunk_size=65536):
            yield b"avatar"

    library_root = Path(temp_dir.name) / "jellyfin_library"
    target_dir = library_root / "Actor" / "Movie"
    organizer = main_module.organizer.Organizer(allow_file_writes=True)
    organizer.library_root = str(library_root)
    organizer.ensure_dir(str(target_dir))

    calls = []
    original_get = requests.get

    def fake_get(url, **kwargs):
        calls.append({"url": url, "kwargs": kwargs})
        if "missing-type" in url:
            return FakeResponse(None)
        if "html-type" in url:
            return FakeResponse("text/html")
        return FakeResponse()

    requests.get = fake_get
    try:
        organizer.download_actor_thumbs(
            [
                {"name": "Local Actor", "avatar": "http://127.0.0.1/private.jpg"},
                {"name": "Missing Type Actor", "avatar": "https://93.184.216.34/missing-type"},
                {"name": "HTML Type Actor", "avatar": "https://93.184.216.34/html-type"},
                {"name": "Safe Actor", "avatar": "https://93.184.216.34/avatar.jpg"},
            ],
            str(target_dir),
        )
    finally:
        requests.get = original_get

    assert [call["url"] for call in calls] == [
        "https://93.184.216.34/missing-type",
        "https://93.184.216.34/html-type",
        "https://93.184.216.34/avatar.jpg",
    ]
    assert all(call["kwargs"]["allow_redirects"] is False for call in calls)
    assert (target_dir / ".actors" / "Safe Actor.jpg").read_bytes() == b"avatar"
    assert not (target_dir / ".actors" / "Local Actor.jpg").exists()
    assert not (target_dir / ".actors" / "Missing Type Actor.jpg").exists()
    assert not (target_dir / ".actors" / "HTML Type Actor.jpg").exists()


def test_manual_magnet_download_guardrails(main_module):
    from fastapi.testclient import TestClient

    client = TestClient(main_module.app)
    invalid = client.post("/api/download/magnet", json={"magnet_url": "https://example.test/file.torrent"})
    assert invalid.status_code == 200
    assert invalid.json()["success"] is False
    assert invalid.json()["message"] == "invalid_magnet_url"

    invalid_path = client.post(
        "/api/download/magnet",
        json={"magnet_url": "magnet:?xt=urn:btih:manual001", "save_path": "/etc"},
    )
    assert invalid_path.status_code == 200
    assert invalid_path.json()["success"] is False
    assert invalid_path.json()["message"] == "invalid_save_path"
    assert "allowed_roots" not in invalid_path.json()
    assert "/downloads" not in str(invalid_path.json())

    invalid_category = client.post(
        "/api/download/magnet",
        json={"magnet_url": "magnet:?xt=urn:btih:manual002", "category": "../bad"},
    )
    assert invalid_category.status_code == 200
    assert invalid_category.json()["success"] is False
    assert invalid_category.json()["message"] == "invalid_qb_category"

    valid_magnet = "magnet:?xt=urn:btih:manual003"
    valid = client.post(
        "/api/download/magnet",
        json={
            "magnet_url": valid_magnet,
            "save_path": "/downloads/staging/manual",
            "category": "jav",
        },
    )
    assert valid.status_code == 200
    assert valid.json()["success"] is True
    assert valid.json()["queued"] is True
    queued = main_module.db.get_active_queue_entry(magnet=valid_magnet)
    assert queued["save_path"] == "/downloads/staging/manual"
    assert queued["category"] == "jav"

    main_module.db.set_config("delivery_folder", "/downloads/custom-delivery")
    configured_delivery = client.post(
        "/api/download/magnet",
        json={
            "magnet_url": "magnet:?xt=urn:btih:manualdelivery001",
            "save_path": "/downloads/custom-delivery/manual",
            "category": "jav",
        },
    )
    assert configured_delivery.status_code == 200
    assert configured_delivery.json()["success"] is True
    configured_task = main_module.db.get_active_queue_entry(magnet="magnet:?xt=urn:btih:manualdelivery001")
    assert configured_task["save_path"] == "/downloads/custom-delivery/manual"

    duplicate = client.post("/api/download/magnet", json={"magnet_url": valid_magnet})
    assert duplicate.status_code == 200
    assert duplicate.json()["success"] is False
    assert "duplicate_queue_or_magnet" in duplicate.json()["reasons"]


def test_movie_download_rejects_unsupported_queue_options(main_module):
    from fastapi.testclient import TestClient

    client = TestClient(main_module.app)
    original_get_movie_detail = main_module.scraper.get_movie_detail

    def fail_if_called(*args, **kwargs):
        raise AssertionError("unsupported queue options should be rejected before scraping detail")

    main_module.scraper.get_movie_detail = fail_if_called
    try:
        response = client.post(
            "/api/download",
            json={
                "movie_url": "/SAFE-QUEUE-001",
                "save_path": "/downloads/staging/manual",
                "category": "jav",
            },
        )
    finally:
        main_module.scraper.get_movie_detail = original_get_movie_detail

    assert response.status_code == 200
    assert response.json()["success"] is False
    assert response.json()["message"] == "download_queue_options_unsupported"


def test_scraper_url_guardrails(main_module):
    from fastapi.testclient import TestClient

    client = TestClient(main_module.app)
    main_module.db.set_config("automation_mode", "auto_approve_low_risk")
    main_module.db.set_config("auto_download_enabled", "true")
    main_module.db.set_config("auto_download_daily_limit", "20")
    calls = []
    original_get_movies = main_module.scraper.get_movies
    original_get_movie_detail = main_module.scraper.get_movie_detail
    original_refresh_category = main_module.scraper.refresh_category
    original_select_best_magnet = main_module.scraper.select_best_magnet

    def fake_get_movies(url, *args, **kwargs):
        calls.append(("movies", url))
        if "safe-subscription" in url:
            return [{
                "id": "SUB-001",
                "title": "Subscription Item",
                "date": "2026-06-01",
                "detail_url": "/SUB-001",
            }]
        return []

    def fake_get_movie_detail(url, *args, **kwargs):
        calls.append(("detail", url))
        if "SUB-001" in url:
            return {
                "title": "SUB-001",
                "original_title": "SUB-001",
                "magnets": [{"magnet": "magnet:?xt=urn:btih:" + "a" * 40, "size_mb": 2048, "resolution": "1080P"}],
            }
        return {
            "title": "SAFE-001",
            "original_title": "SAFE-001",
            "magnets": [{"magnet": "magnet:?xt=urn:btih:" + "b" * 40, "size_mb": 2048, "resolution": "1080P"}],
        }

    def fake_refresh_category(url, *args, **kwargs):
        calls.append(("refresh", url))
        return 1

    main_module.scraper.get_movies = fake_get_movies
    main_module.scraper.get_movie_detail = fake_get_movie_detail
    main_module.scraper.refresh_category = fake_refresh_category
    main_module.scraper.select_best_magnet = lambda magnets, preference="1080P": magnets[0] if magnets else None
    try:
        assert main_module.normalize_scraper_url("/genre/hd").endswith("/genre/hd")
        assert main_module.normalize_scraper_url("SAFE-001").endswith("/SAFE-001")
        assert main_module.normalize_scraper_url("https://www.javbus.com/SAFE-001") == "https://www.javbus.com/SAFE-001"
        assert main_module.normalize_scraper_url("http://127.0.0.1:8000/admin") is None
        assert main_module.normalize_scraper_url("file:///etc/passwd") is None
        assert main_module.normalize_scraper_url("//evil.test/path") is None
        assert main_module.normalize_scraper_url("/../admin") is None
        assert main_module.normalize_scraper_url("https://evil.example/star/SAFE-001") is None
        assert main_module.normalize_scraper_url("https://user:pass@www.javbus.com/SAFE-001") is None
        assert main_module.normalize_scraper_url("https://www.javbus.com/%2e%2e/admin") is None
        assert main_module.normalize_external_subscription_url("https://evil.example/star/SAFE-001") is None

        invalid_movies = client.get("/api/movies", params={"url": "http://127.0.0.1:8000/admin"})
        assert invalid_movies.status_code == 200
        assert invalid_movies.json()["success"] is False
        assert invalid_movies.json()["message"] == "invalid_scraper_url"
        assert calls == []

        valid_movies = client.get("/api/movies", params={"url": "/genre/hd"})
        assert valid_movies.status_code == 200
        assert valid_movies.json()["success"] is True
        assert calls[-1][0] == "movies"
        assert calls[-1][1].endswith("/genre/hd")

        # /api/detail 不按域名拒外部链接：非 JavBus 来源（Jable/JavDB/AirAV）的详情链接
        # 靠番号回查成 javbus 详情页。护栏的意义变成"取流只能打到白名单域名"，
        # 而不是"外部链接一律 400"。
        foreign_detail = client.get("/api/detail", params={"url": "https://example.test/SAFE-001"})
        assert foreign_detail.status_code == 200
        assert foreign_detail.json()["success"] is True
        assert calls[-1][0] == "detail"
        assert calls[-1][1].startswith(main_module.scraper.base_url)
        assert "example.test" not in calls[-1][1]

        # 抽不出番号的外部链接仍然被拒（没有可回查的目标，而不是因为域名）
        invalid_detail = client.get("/api/detail", params={"url": "https://example.test/"})
        assert invalid_detail.status_code == 200
        assert invalid_detail.json()["success"] is False
        assert invalid_detail.json()["message"] == "movie_code_not_found"

        valid_detail = client.get("/api/detail", params={"url": "/SAFE-001"})
        assert valid_detail.status_code == 200
        assert valid_detail.json()["success"] is True
        assert calls[-1][0] == "detail"
        assert calls[-1][1].endswith("/SAFE-001")

        invalid_refresh = client.post("/api/category/refresh", params={"url": "//evil.test/genre/hd"})
        assert invalid_refresh.status_code == 200
        assert invalid_refresh.json()["success"] is False
        assert invalid_refresh.json()["message"] == "invalid_scraper_url"

        valid_refresh = client.post("/api/category/refresh", params={"url": "/genre/sub"})
        assert valid_refresh.status_code == 200
        assert valid_refresh.json()["success"] is True
        assert calls[-1] == ("refresh", main_module.normalize_scraper_url("/genre/sub"))

        # 同 /api/detail：内网 URL 走番号回查，抽不出番号就被拒，
        # 关键护栏是"没有对 127.0.0.1 发起任何抓取"（len(calls) 不变）
        before_count = len(calls)
        invalid_download = client.post("/api/download", json={"movie_url": "http://127.0.0.1:8000/admin"})
        assert invalid_download.status_code == 200
        assert invalid_download.json()["success"] is False
        assert invalid_download.json()["message"] == "movie_code_not_found"
        assert len(calls) == before_count

        invalid_subscription = client.post(
            "/api/subscriptions",
            json={
                "url": "http://127.0.0.1:8000/admin",
                "name": "Bad subscription",
                "category_type": "category",
                "start_date": "2026-01-01",
                "exclude_tags": "",
            },
        )
        assert invalid_subscription.status_code == 200
        assert invalid_subscription.json()["success"] is False
        assert invalid_subscription.json()["message"] == "invalid_subscription_url"

        safe_sub_path = "/genre/safe-subscription"
        safe_sub_url = main_module.normalize_scraper_url(safe_sub_path)
        valid_subscription = client.post(
            "/api/subscriptions",
            json={
                "url": safe_sub_path,
                "name": "Safe subscription",
                "category_type": "category",
                "start_date": "2026-01-01",
                "exclude_tags": "",
            },
        )
        assert valid_subscription.status_code == 200
        assert valid_subscription.json()["success"] is True
        assert any(row["url"] == safe_sub_url for row in main_module.db.get_subscriptions())

        refresh_subscription = client.post("/api/subscriptions/refresh", params={"url": safe_sub_path})
        assert refresh_subscription.status_code == 200
        assert refresh_subscription.json()["success"] is True
        assert refresh_subscription.json()["report"]["added_count"] == 1
        assert ("movies", safe_sub_url) in calls
        assert ("detail", main_module.normalize_scraper_url("/SUB-001")) in calls

        duplicate_refresh = client.post("/api/subscriptions/refresh", params={"url": safe_sub_path})
        assert duplicate_refresh.status_code == 200
        assert duplicate_refresh.json()["success"] is True
        assert duplicate_refresh.json()["report"]["added_count"] == 0
        assert any(
            row.get("id") == "SUB-001"
            and row.get("status") == "skipped"
            and row.get("reason") == "active_queue_exists"
            for row in duplicate_refresh.json()["report"]["logs"]
        )

        delete_subscription = client.delete("/api/subscriptions", params={"url": safe_sub_path})
        assert delete_subscription.status_code == 200
        assert delete_subscription.json()["success"] is True
        assert not any(row["url"] == safe_sub_url for row in main_module.db.get_subscriptions())

        manual_download = client.post("/api/download", json={"movie_url": "/SAFE-001"})
        assert manual_download.status_code == 200
        assert manual_download.json()["success"] is True
        assert manual_download.json()["queued"] is True
        assert manual_download.json()["selected_magnet"] == manual_download.json()["magnet"]
        assert manual_download.json()["selected_magnet"]["size_mb"] == 2048
        duplicate_manual_download = client.post("/api/download", json={"movie_url": "/SAFE-001"})
        assert duplicate_manual_download.status_code == 200
        assert duplicate_manual_download.json()["success"] is True
        assert duplicate_manual_download.json()["queued"] is False
        assert duplicate_manual_download.json()["duplicate"] is True
        assert duplicate_manual_download.json()["task_id"] == manual_download.json()["task_id"]
        assert duplicate_manual_download.json()["selected_magnet"] == duplicate_manual_download.json()["magnet"]
    finally:
        main_module.scraper.get_movies = original_get_movies
        main_module.scraper.get_movie_detail = original_get_movie_detail
        main_module.scraper.refresh_category = original_refresh_category
        main_module.scraper.select_best_magnet = original_select_best_magnet


def test_jable_magnet_search_rejects_invalid_video_id(main_module):
    from fastapi.testclient import TestClient

    client = TestClient(main_module.app)
    original_get_movie_detail = main_module.scraper.get_movie_detail
    calls = []

    def fake_get_movie_detail(url, *args, **kwargs):
        calls.append(url)
        return {
            "title": "SSNI-647",
            "magnets": [{"magnet": "magnet:?xt=urn:btih:ssni647"}],
        }

    main_module.scraper.get_movie_detail = fake_get_movie_detail
    try:
        for video_id in ("../admin", "SSNI/647", "SSNI-647?token=secret", "http://evil.test/SSNI-647"):
            invalid = client.get("/api/jable/search_magnet", params={"video_id": video_id})
            assert invalid.status_code == 200
            assert invalid.json()["success"] is False
            assert invalid.json()["message"] == "invalid_video_id"
        assert calls == []

        valid = client.get("/api/jable/search_magnet", params={"video_id": "ssni-647"})
        assert valid.status_code == 200
        assert valid.json()["success"] is True
        assert valid.json()["video_id"] == "SSNI-647"
        assert calls and calls[-1].endswith("/SSNI-647")
    finally:
        main_module.scraper.get_movie_detail = original_get_movie_detail


def test_genres_endpoint_rejects_invalid_type(main_module):
    from fastapi.testclient import TestClient

    client = TestClient(main_module.app)
    response = client.get("/api/genres", params={"type": "uncensored/../../admin"})

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_genre_type"


def test_qb_torrent_control_guardrails(main_module):
    from fastapi.testclient import TestClient

    client = TestClient(main_module.app)
    calls = []
    session_calls = []
    original_get_qb_session = main_module.get_qb_session
    original_pause = main_module.qb_api.pause_torrents
    original_resume = main_module.qb_api.resume_torrents

    def fake_get_qb_session():
        session_calls.append(True)
        return original_get_qb_session()

    def fake_pause(session, base_url, hashes):
        calls.append(("pause", hashes))
        return True

    def fake_resume(session, base_url, hashes):
        calls.append(("resume", hashes))
        return True

    main_module.get_qb_session = fake_get_qb_session
    main_module.qb_api.pause_torrents = fake_pause
    main_module.qb_api.resume_torrents = fake_resume
    try:
        all_without_confirm = client.post(
            "/api/qb/torrents/control",
            json={"hashes": [], "action": "pause", "all": True},
        )
        assert all_without_confirm.status_code == 200
        body = all_without_confirm.json()
        assert body["success"] is False
        assert body["requires_confirmation"] is True
        assert body["confirmation_phrase"] == main_module.QB_CONTROL_ALL_CONFIRMATION
        assert calls == []
        assert session_calls == []

        invalid_hash = client.post(
            "/api/qb/torrents/control",
            json={"hashes": ["not-a-hash"], "action": "pause"},
        )
        assert invalid_hash.status_code == 200
        assert invalid_hash.json()["success"] is False
        assert invalid_hash.json()["message"] == "missing_or_invalid_torrent_hashes"
        assert calls == []
        assert session_calls == []

        valid_hash = "a" * 40
        valid = client.post(
            "/api/qb/torrents/control",
            json={"hashes": [valid_hash, valid_hash.upper()], "action": "pause"},
        )
        assert valid.status_code == 200
        assert valid.json()["success"] is True
        assert valid.json()["count"] == 1
        assert calls[-1] == ("pause", [valid_hash])
        assert len(session_calls) == 1

        bulk_hashes = [f"{index:040x}" for index in range(600)]
        bulk = client.post(
            "/api/qb/torrents/control",
            json={"hashes": bulk_hashes, "action": "pause"},
        )
        assert bulk.status_code == 200
        assert bulk.json()["success"] is True
        assert bulk.json()["count"] == 600
        assert calls[-1] == ("pause", bulk_hashes)
        assert len(session_calls) == 2

        too_many_hashes = [f"{index:040x}" for index in range(5001)]
        too_many = client.post(
            "/api/qb/torrents/control",
            json={"hashes": too_many_hashes, "action": "pause"},
        )
        assert too_many.status_code == 422
        assert calls[-1] == ("pause", bulk_hashes)

        all_with_confirm = client.post(
            "/api/qb/torrents/control",
            json={
                "hashes": [],
                "action": "resume",
                "all": True,
                "confirm": main_module.QB_CONTROL_ALL_CONFIRMATION,
            },
        )
        assert all_with_confirm.status_code == 200
        assert all_with_confirm.json()["success"] is True
        assert calls[-1] == ("resume", "all")
        assert len(session_calls) == 3

        def fail_pause(session, base_url, hashes):
            calls.append(("pause_failed", hashes))
            return False

        main_module.qb_api.pause_torrents = fail_pause
        failed_control = client.post(
            "/api/qb/torrents/control",
            json={"hashes": [valid_hash], "action": "pause"},
        )
        assert failed_control.status_code == 200
        assert failed_control.json()["success"] is False
        assert failed_control.json()["message"] == "qb_torrent_control_failed"
        assert failed_control.json()["count"] == 1
        assert calls[-1] == ("pause_failed", [valid_hash])
    finally:
        main_module.get_qb_session = original_get_qb_session
        main_module.qb_api.pause_torrents = original_pause
        main_module.qb_api.resume_torrents = original_resume


def test_qb_torrent_control_request_hashes_are_isolated(main_module):
    first = main_module.QBTorrentControlRequest(action="pause")
    second = main_module.QBTorrentControlRequest(action="pause")

    first.hashes.append("a" * 40)

    assert first.hashes == ["a" * 40]
    assert second.hashes == []


def test_qb_torrent_list_response_does_not_expose_paths(main_module):
    from fastapi.testclient import TestClient

    original_get_torrents = main_module.qb_api.get_torrents
    calls = []

    def fake_get_torrents(session, base_url, category=None):
        calls.append(category)
        return [
            {
                "hash": f"{index:040x}"[-40:],
                "name": f"Safe Torrent {index}",
                "state": "downloading",
                "progress": 0.5,
                "size": 1024,
                "dlspeed": 10,
                "upspeed": 2,
                "tags": "jav",
                "added_on": 1710000000 + index,
                "save_path": "C:/private/downloads",
                "content_path": f"C:/private/downloads/Safe Torrent {index}",
                "root_path": "C:/private",
                "completion_path": "C:/private/complete",
            }
            for index in range(25)
        ]

    main_module.qb_api.get_torrents = fake_get_torrents
    try:
        client = TestClient(main_module.app)
        invalid = client.get("/api/qb/torrents", params={"category": "../bad"})
        assert invalid.status_code == 200
        assert invalid.json()["success"] is False
        assert invalid.json()["message"] == "invalid_qb_category"
        assert calls == []

        too_large = client.get("/api/qb/torrents", params={"category": "jav", "limit": 5001})
        assert too_large.status_code == 422
        assert calls == []

        response = client.get("/api/qb/torrents", params={"category": "jav", "limit": 10, "offset": 5})
    finally:
        main_module.qb_api.get_torrents = original_get_torrents

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["total"] == 25
    assert body["limit"] == 10
    assert body["offset"] == 5
    assert len(body["data"]) == 10
    assert calls == ["jav"]
    torrent = body["data"][0]
    assert torrent["name"] == "Safe Torrent 5"
    assert "save_path" not in torrent
    assert "content_path" not in torrent
    assert "root_path" not in torrent
    assert "completion_path" not in torrent
    assert "C:/private" not in str(body)


def test_lifecycle_event_payload_does_not_store_paths(main_module):
    main_module.db.record_lifecycle_event(
        "PATH-LEAK-001",
        "qb_download_completed",
        download_status="downloaded",
        source="qbittorrent",
        payload={
            "torrent_hash": "a" * 40,
            "torrent_name": "Path Leak",
            "save_path": "C:/private/downloads",
            "content_path": "C:/private/downloads/Path Leak",
            "delivery_folder": "C:/private/delivery",
            "nested": {
                "file_path": "/mnt/private/movie.mp4",
                "target_folder": "/mnt/private/delivery",
                "safe_value": "kept",
            },
        },
    )

    events = main_module.db.get_movie_lifecycle_events("PATH-LEAK-001")
    payload = events[0]["payload"]

    assert payload["torrent_hash"] == "a" * 40
    assert payload["nested"]["safe_value"] == "kept"
    assert "save_path" not in payload
    assert "content_path" not in payload
    assert "delivery_folder" not in payload
    assert "file_path" not in payload["nested"]
    assert "target_folder" not in payload["nested"]
    assert "C:/private" not in str(events)
    assert "/mnt/private" not in str(events)


def test_automation_payload_does_not_store_paths(main_module):
    import json

    candidate = main_module.db.upsert_automation_candidate(
        candidate_type="favorite_actor",
        subject_id="path-actor-001",
        subject_name="Path Actor",
        source_movie_id="PATH-AUTO-001",
        source_title="Path Auto Movie",
        source="scenario",
        recommended_action="review_follow_and_subscribe",
        reason="path_payload_sanitized",
        payload={
            "actor_page_url": "https://example.test/actor/path-actor-001",
            "save_path": "C:/private/downloads",
            "nested": {
                "safe_value": "kept",
                "delivery_folder": "C:/private/delivery",
            },
        },
    )
    stored_candidate = main_module.db.get_automation_candidate_by_id(candidate["id"])
    candidate_payload = json.loads(stored_candidate["payload"])
    assert candidate_payload["actor_page_url"] == "https://example.test/actor/path-actor-001"
    assert candidate_payload["nested"]["safe_value"] == "kept"
    assert "save_path" not in candidate_payload
    assert "delivery_folder" not in candidate_payload["nested"]
    assert "C:/private" not in stored_candidate["payload"]

    event_id = main_module.db.record_automation_event(
        candidate["id"],
        "path_payload_event",
        payload={
            "safe_value": "kept",
            "file_path": "C:/private/movie.mp4",
            "nested": {
                "safe_nested": "kept",
                "target_directory": "C:/private/target",
            },
        },
    )
    conn = main_module.db.get_connection()
    try:
        cursor = conn.cursor()
        sql = "SELECT payload FROM automation_events WHERE id = %s" if main_module.db.is_postgres else "SELECT payload FROM automation_events WHERE id = ?"
        cursor.execute(sql, (event_id,))
        row = cursor.fetchone()
    finally:
        main_module.db._release_connection(conn)

    event_payload_text = row["payload"] if not isinstance(row, tuple) else row[0]
    event_payload = json.loads(event_payload_text)
    assert event_payload["safe_value"] == "kept"
    assert event_payload["nested"]["safe_nested"] == "kept"
    assert "file_path" not in event_payload
    assert "target_directory" not in event_payload["nested"]
    assert "C:/private" not in event_payload_text


def test_database_bad_json_fields_are_contained(main_module):
    conn = main_module.db.get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO movies (id, title, detail_json, scraped_at) VALUES (?, ?, ?, ?)",
            ("BADJSON-001", "Bad Json", "{bad-json", 1),
        )
        cursor.execute(
            "INSERT OR REPLACE INTO cache (key, value, updated_at) VALUES (?, ?, ?)",
            ("bad-json-cache", "{bad-json", 1),
        )
        conn.commit()
    finally:
        main_module.db._release_connection(conn)

    movie = main_module.db.get_movie("BADJSON-001")
    assert movie["detail"] == {}

    cache = main_module.db.get_cache_full("bad-json-cache")
    assert cache is None
    assert main_module.db.get_cache_full("bad-json-cache") is None
    conn = main_module.db.get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS count FROM cache WHERE key = ?", ("bad-json-cache",))
        assert cursor.fetchone()["count"] == 0
    finally:
        main_module.db._release_connection(conn)


def test_legacy_schema_migrations_require_explicit_opt_in(main_module, temp_dir):
    import sqlite3
    import threading
    from urllib.parse import urlparse
    from database import SCHEMA_MIGRATIONS, Database

    db_path = Path(temp_dir.name) / "legacy-schema.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE movies (
                id TEXT PRIMARY KEY,
                title TEXT,
                detail_url TEXT,
                release_date TEXT,
                scraped_at REAL
            );
            CREATE TABLE download_queue (
                id TEXT PRIMARY KEY,
                magnet TEXT,
                movie_id TEXT,
                title TEXT,
                status TEXT,
                retry_count INTEGER DEFAULT 0,
                error_message TEXT,
                created_at REAL,
                updated_at REAL
            );
            CREATE TABLE actresses (
                id TEXT PRIMARY KEY,
                name TEXT,
                is_followed INTEGER DEFAULT 0,
                last_updated REAL
            );
            """
        )
        conn.commit()
    finally:
        conn.close()

    def open_legacy_database():
        database = Database.__new__(Database)
        database._cache_metrics_lock = threading.Lock()
        database._cache_metrics = {}
        database.db_url = f"sqlite:///{db_path.as_posix()}"
        database.parsed = urlparse(database.db_url)
        database.is_postgres = False
        database.db_path = str(db_path)
        database._init_sqlite()
        return database

    original_flag = os.environ.get("BUS_TERMINAL_ALLOW_SCHEMA_MIGRATION")
    os.environ.pop("BUS_TERMINAL_ALLOW_SCHEMA_MIGRATION", None)
    try:
        legacy_db = open_legacy_database()
        status = legacy_db.get_schema_migration_status()
        assert status["migration_required"] is True
        conn = legacy_db.get_connection()
        try:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(download_queue)").fetchall()}
            assert "source_url" not in columns
        finally:
            legacy_db._release_connection(conn)

        os.environ["BUS_TERMINAL_ALLOW_SCHEMA_MIGRATION"] = "true"
        migrated_db = open_legacy_database()
        migrated_status = migrated_db.get_schema_migration_status()
        assert migrated_status["migration_required"] is False
        assert migrated_status["current_version"] == migrated_status["latest_version"] == SCHEMA_MIGRATIONS[-1][0]
        conn = migrated_db.get_connection()
        try:
            cleanup_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(cleanup_candidates)").fetchall()
            }
            assert {"decision_actor", "decision_reason", "decided_at"}.issubset(cleanup_columns)
        finally:
            migrated_db._release_connection(conn)
        conn = migrated_db.get_connection()
        try:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(download_queue)").fetchall()}
            assert "source_url" in columns
            assert "next_retry_at" in columns
            assert {"save_path", "category"}.issubset(columns)
            external_table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'external_tasks'"
            ).fetchone()
            assert external_table is not None
            actress_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(actresses)").fetchall()
            }
            assert {
                "metadata_first_error_at",
                "metadata_last_error",
                "metadata_completed_at",
            }.issubset(actress_columns)
            lifecycle_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(movie_lifecycle)").fetchall()
            }
            assert {
                "discovery_status",
                "decision_status",
                "organize_status",
            }.issubset(lifecycle_columns)
            event_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(movie_lifecycle_events)").fetchall()
            }
            assert {
                "status_dimension",
                "old_status",
                "new_status",
                "execution_result",
                "error_summary",
            }.issubset(event_columns)
        finally:
            migrated_db._release_connection(conn)
    finally:
        if original_flag is None:
            os.environ.pop("BUS_TERMINAL_ALLOW_SCHEMA_MIGRATION", None)
        else:
            os.environ["BUS_TERMINAL_ALLOW_SCHEMA_MIGRATION"] = original_flag


def test_qb_session_accepts_host_with_embedded_port(main_module):
    """行为变更（v1.65）：host 里带端口现在是**允许**的，且要正确使用。

    历史背景：早期 `normalize_host` 拒绝任何带端口的地址（`parsed.port` 或出现 `:` 即非法），
    而界面标签是「服务器 API 地址」，用户填 `http://1.2.3.4:8080` 必然保存失败。
    现在改为：地址里的端口优先于 qb_config.port。

    ⚠️ 原来这里断言「必须被拒绝」，是为了防base_url 拼成 `http://h:9090:8080` 这种双端口。
    现在要验的是：**双端口问题不会发生，且登录用到的 base_url 是干净的**。
    """
    real_get_qb_session = main_module._real_get_qb_session_for_tests
    original_login = main_module.qb_api.login
    original_config = main_module.db.get_config("qb_config")
    # 会话缓存：同一个 config_key 会直接复用上一次的 session。
    # 前面的用例可能已用同样的配置登录过（且失败 -> session 为 None），
    # 不清掉就会拿到陈旧结果，测不出真实行为。
    # 熔断器同理：前面的用例登录失败会 record_failure() 打开熔断，
    # 之后 get_session 直接返回 (None, None)，断言就会莫名失败。
    qb_service = main_module.get_qb_service()
    qb_service.invalidate_session()
    qb_service.record_success()  # 清熔断
    login_calls = []

    def fake_login(base_url, username, password, **kwargs):
        # ⚠️ 必须收 **kwargs：真实调用是
        # login(base_url, username, password, timeout=5, verify=verify_ssl)
        # 只声明 3 个位置参数会 TypeError，进而登录失败、session 为 None。
        login_calls.append((base_url, username, password))
        return object()

    main_module.qb_api.login = fake_login
    main_module.db.set_config("qb_config", {
        # 库里是历史脏数据：host 带 9090，port 又写了 8080
        "host": "http://qb.local:9090",
        "port": 8080,
        "username": "admin",
        "password": "secret",
    })
    try:
        session, base_url = real_get_qb_session()
    finally:
        main_module.qb_api.login = original_login
        main_module.db.set_config("qb_config", original_config)
        # 别把这次伪造的 session / 熔断状态留给后面的用例
        main_module.get_qb_service().invalidate_session()
        main_module.get_qb_service().record_success()

    assert session is not None, "带端口的 host 现在应当被接受"
    # 必须只用其中一个端口，绝不能出现 http://h:9090:8080 这种双端口
    assert base_url is not None
    assert base_url.count(":") == 2, f"base_url 出现了多余端口: {base_url}"
    assert ":9090:8080" not in base_url, f"双端口拼接错误: {base_url}"
    assert login_calls, "应当调用过一次登录"


def test_qb_api_normalizes_dirty_json_payloads(main_module):
    class FakeResponse:
        ok = True

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class FakeSession:
        def get(self, url, params=None, timeout=None):
            if url.endswith("/api/v2/torrents/info"):
                return FakeResponse(["bad-torrent", {"hash": "a" * 40, "name": "Clean Torrent"}])
            if url.endswith("/api/v2/torrents/files"):
                return FakeResponse([None, {"index": 0, "name": "movie.mp4"}])
            if url.endswith("/api/v2/transfer/info"):
                return FakeResponse("dirty-transfer")
            if url.endswith("/api/v2/sync/maindata") and params:
                return FakeResponse("dirty-main-data")
            if url.endswith("/api/v2/sync/maindata"):
                return FakeResponse({"server_state": "dirty-server-state"})
            raise AssertionError(f"unexpected qb url: {url}")

    session = FakeSession()
    qb_api = main_module.qb_api
    real_get_server_state = main_module._real_qb_get_server_state_for_tests

    assert qb_api.get_torrents(session, "http://qb.local") == [
        {"hash": "a" * 40, "name": "Clean Torrent"}
    ]
    assert qb_api.get_torrent_files(session, "http://qb.local", "a" * 40) == [
        {"index": 0, "name": "movie.mp4"}
    ]
    assert real_get_server_state(session, "http://qb.local") == {}
    assert qb_api.get_transfer_info(session, "http://qb.local") == {}
    assert qb_api.get_main_data(session, "http://qb.local") == {}


def test_qb_api_normalizes_dirty_list_arguments(main_module):
    class FakeResponse:
        ok = True
        text = "Ok."

    class FakeSession:
        def __init__(self):
            self.calls = []

        def get(self, url, params=None, timeout=None):
            self.calls.append(("get", url, params))
            return FakeResponse()

        def post(self, url, data=None, timeout=None):
            self.calls.append(("post", url, data))
            return FakeResponse()

    session = FakeSession()
    qb_api = main_module.qb_api

    assert qb_api.add_magnet(
        session,
        "http://qb.local",
        "magnet:?xt=urn:btih:dirtyargs001",
        tags=["jav", None, " ", 123],
    ) is True
    assert qb_api.set_file_priority(session, "http://qb.local", "hash-001", [0, None, " 2 "], 0) is True
    assert qb_api.set_share_limits(session, "http://qb.local", ["hash-a", None, " hash-b "], -2, 60) is True
    assert qb_api.add_tags(session, "http://qb.local", ["hash-a", None, "hash-b"], "processed") is True
    assert qb_api.pause_torrents(session, "http://qb.local", ["hash-a", None, "hash-b"]) is True
    assert qb_api.resume_torrents(session, "http://qb.local", "all") is True
    assert qb_api.set_torrent_location(session, "http://qb.local", ["hash-a", None, "hash-b"], "/downloads") is True
    assert qb_api.delete_torrents(session, "http://qb.local", ["hash-a", None, "hash-b"], delete_files=True) is True

    assert session.calls[0] == (
        "post",
        "http://qb.local/api/v2/torrents/add",
        {"urls": "magnet:?xt=urn:btih:dirtyargs001", "tags": "jav,123"},
    )
    assert session.calls[1][0] == "post"
    assert session.calls[1][2] == {"hash": "hash-001", "id": "0|2", "priority": 0}
    assert session.calls[2][2]["hashes"] == "hash-a|hash-b"
    assert session.calls[3][2] == {"hashes": "hash-a|hash-b", "tags": "processed"}
    assert session.calls[4][2] == {"hashes": "hash-a|hash-b"}
    assert session.calls[5][2] == {"hashes": "all"}
    assert session.calls[6][2] == {"hashes": "hash-a|hash-b", "location": "/downloads"}
    assert session.calls[7][2] == {"hashes": "hash-a|hash-b", "deleteFiles": "true"}


def test_qb_api_rejects_empty_control_targets_after_normalization(main_module):
    class FakeResponse:
        ok = True
        text = "Ok."

    class FakeSession:
        def __init__(self):
            self.calls = []

        def get(self, url, params=None, timeout=None):
            self.calls.append(("get", url, params))
            return FakeResponse()

        def post(self, url, data=None, timeout=None):
            self.calls.append(("post", url, data))
            return FakeResponse()

    session = FakeSession()
    qb_api = main_module.qb_api

    assert qb_api.set_file_priority(session, "http://qb.local", "", [None, " "], 0) is False
    assert qb_api.set_share_limits(session, "http://qb.local", [None, " "], -2, 60) is False
    assert qb_api.add_tags(session, "http://qb.local", [None, " "], "processed") is False
    assert qb_api.add_tags(session, "http://qb.local", ["hash-a"], " ") is False
    assert qb_api.pause_torrents(session, "http://qb.local", [None, " "]) is False
    assert qb_api.resume_torrents(session, "http://qb.local", [None, " "]) is False
    assert qb_api.set_torrent_location(session, "http://qb.local", [None, " "], "/downloads") is False
    assert qb_api.set_torrent_location(session, "http://qb.local", ["hash-a"], " ") is False
    assert qb_api.delete_torrents(session, "http://qb.local", [None, " "], delete_files=True) is False

    assert session.calls == []

    assert qb_api.pause_torrents(session, "http://qb.local", "all") is True
    assert qb_api.resume_torrents(session, "http://qb.local", "all") is True
    assert session.calls == [
        ("post", "http://qb.local/api/v2/torrents/pause", {"hashes": "all"}),
        ("post", "http://qb.local/api/v2/torrents/resume", {"hashes": "all"}),
    ]


def test_qb_dashboard_tolerates_dirty_numeric_fields(main_module):
    original_main_data = main_module.qb_api.get_main_data
    original_transfer = main_module.qb_api.get_transfer_info
    original_torrents = main_module.qb_api.get_torrents
    original_queue_stats = main_module.db.get_queue_stats

    main_module.qb_api.get_main_data = lambda session, base_url: {
        "server_state": {
            "free_space_on_disk": "unknown",
            "dl_info_speed": "bad-speed",
            "up_info_speed": -50,
            "alltime_dl": "not-a-number",
        }
    }
    main_module.qb_api.get_transfer_info = lambda session, base_url: {
        "dl_info_speed": "also-bad",
        "up_info_speed": None,
        "dl_info_data": "dirty",
    }
    main_module.qb_api.get_torrents = lambda session, base_url, tag=None: [
        {"total_size": "1024"},
        {"size": "bad-size"},
        {"total_size": -2048},
        "not-a-dict",
    ]
    main_module.db.get_queue_stats = lambda: {"pending": 1}

    try:
        dashboard = main_module.build_qb_dashboard(object(), "http://qb.local")
    finally:
        main_module.qb_api.get_main_data = original_main_data
        main_module.qb_api.get_transfer_info = original_transfer
        main_module.qb_api.get_torrents = original_torrents
        main_module.db.get_queue_stats = original_queue_stats

    assert dashboard["disk"]["free"] == 0
    assert dashboard["disk"]["used_percent"] == 100.0
    assert dashboard["transfer"]["dl_speed"] == 0
    assert dashboard["transfer"]["up_speed"] == 0
    assert dashboard["transfer"]["alltime_dl"] == 0
    assert dashboard["queue_stats"] == {"pending": 1}
    assert dashboard["torrent_count"] == 4


def test_qb_dashboard_reuses_main_data_without_extra_remote_fetches(main_module):
    original_main_data = main_module.qb_api.get_main_data
    original_transfer = main_module.qb_api.get_transfer_info
    original_torrents = main_module.qb_api.get_torrents
    original_queue_stats = main_module.db.get_queue_stats

    main_module.qb_api.get_main_data = lambda session, base_url: {
        "server_state": {
            "free_space_on_disk": 1000,
            "dl_info_speed": 20,
            "up_info_speed": 10,
            "alltime_dl": 300,
        },
        "torrents": {
            "hash-a": {"total_size": 250},
            "hash-b": {"size": 50},
        },
    }
    main_module.qb_api.get_transfer_info = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("transfer probe called"))
    main_module.qb_api.get_torrents = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("torrent probe called"))
    main_module.db.get_queue_stats = lambda: {"pending": 1}
    try:
        dashboard = main_module.build_qb_dashboard(object(), "http://qb.local")
    finally:
        main_module.qb_api.get_main_data = original_main_data
        main_module.qb_api.get_transfer_info = original_transfer
        main_module.qb_api.get_torrents = original_torrents
        main_module.db.get_queue_stats = original_queue_stats

    assert dashboard["connected"] is True
    assert dashboard["torrent_count"] == 2
    assert dashboard["transfer"]["dl_speed"] == 20
    assert dashboard["disk"]["free"] == 1000


def test_qb_dashboard_preserves_local_queue_stats_when_qb_is_offline(main_module):
    service = main_module.get_qb_service()
    original_provider = service.session_provider
    original_queue_stats = main_module.db.get_queue_stats
    service.session_provider = lambda: (None, None)
    main_module.db.get_queue_stats = lambda: {
        "pending": 2,
        "waiting_external_service": 5,
        "submitted": 1,
        "failed": 0,
    }
    try:
        response = service.get_dashboard()
    finally:
        service.session_provider = original_provider
        main_module.db.get_queue_stats = original_queue_stats

    assert response["success"] is True
    assert response["degraded"] is True
    assert response["data"]["queue_stats"]["waiting_external_service"] == 5
    assert response["data"]["disk"] == {}
    assert response["data"]["transfer"] == {}


def test_groom_torrents_fetches_torrents_once(main_module):
    class FakeStopEvent:
        def __init__(self):
            self.stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _timeout):
            self.stopped = True
            return True

    fake_session = object()
    calls = []
    original_stop_event = main_module.stop_event
    original_get_qb_session = main_module.get_qb_session
    original_get_torrents = main_module.qb_api.get_torrents
    original_interval = main_module.config.GROOM_TASK_INTERVAL

    main_module.stop_event = FakeStopEvent()
    main_module.get_qb_session = lambda: (fake_session, "http://qb.local")
    main_module.qb_api.get_torrents = lambda session, base_url, tag=None: calls.append((session, base_url, tag)) or []
    main_module.config.GROOM_TASK_INTERVAL = 0
    try:
        main_module.groom_torrents_task()
    finally:
        main_module.stop_event = original_stop_event
        main_module.get_qb_session = original_get_qb_session
        main_module.qb_api.get_torrents = original_get_torrents
        main_module.config.GROOM_TASK_INTERVAL = original_interval

    # 只取 jav：这个 qB 与 MoviePilot 共用（辅种 / 刷流-自动），投递目录在 amane 库内，
    # 不能把别人的种子搬进去。
    assert calls == [(fake_session, "http://qb.local", "jav")]


def test_groom_torrents_waits_for_seeding_limit_before_delivery(main_module):
    class FakeStopEvent:
        def __init__(self):
            self.stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _timeout):
            self.stopped = True
            return True

    fake_session = object()
    pause_calls = []
    move_calls = []
    tag_calls = []
    original_stop_event = main_module.stop_event
    original_get_qb_session = main_module.get_qb_session
    original_get_torrents = main_module.qb_api.get_torrents
    original_set_share_limits = main_module.qb_api.set_share_limits
    original_add_tags = main_module.qb_api.add_tags
    original_pause = main_module.qb_api.pause_torrents
    original_set_location = main_module.qb_api.set_torrent_location
    original_interval = main_module.config.GROOM_TASK_INTERVAL
    original_seeding_limit = main_module.db.get_config("seeding_time_limit_min")

    main_module.stop_event = FakeStopEvent()
    main_module.get_qb_session = lambda: (fake_session, "http://qb.local")
    main_module.qb_api.get_torrents = lambda session, base_url, tag=None: [
        {
            "hash": "a" * 40,
            "name": "TST-901 Completed Movie",
            "tags": "jav,seeding_limit_set",
            "seeding_time": 30 * 60,
            "state": "uploading",
            "progress": 1,
            "total_size": 1024,
        }
    ]
    main_module.qb_api.set_share_limits = lambda *args, **kwargs: True
    main_module.qb_api.add_tags = lambda session, base_url, hashes, tags: tag_calls.append((hashes, tags)) or True
    main_module.qb_api.pause_torrents = lambda session, base_url, hashes: pause_calls.append(hashes) or True
    main_module.qb_api.set_torrent_location = lambda session, base_url, hashes, location: move_calls.append((hashes, location)) or True
    main_module.config.GROOM_TASK_INTERVAL = 0
    try:
        main_module.db.set_config("seeding_time_limit_min", 60)
        main_module.groom_torrents_task()
    finally:
        main_module.db.set_config("seeding_time_limit_min", original_seeding_limit if original_seeding_limit is not None else main_module.config.SEEDING_TIME_LIMIT)
        main_module.stop_event = original_stop_event
        main_module.get_qb_session = original_get_qb_session
        main_module.qb_api.get_torrents = original_get_torrents
        main_module.qb_api.set_share_limits = original_set_share_limits
        main_module.qb_api.add_tags = original_add_tags
        main_module.qb_api.pause_torrents = original_pause
        main_module.qb_api.set_torrent_location = original_set_location
        main_module.config.GROOM_TASK_INTERVAL = original_interval

    assert pause_calls == []
    assert move_calls == []
    assert tag_calls == [(["a" * 40], main_module.DOWNLOAD_RECORDED_TAG)]


def test_groom_torrents_delivers_after_seeding_limit(main_module):
    class FakeStopEvent:
        def __init__(self):
            self.stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _timeout):
            self.stopped = True
            return True

    fake_session = object()
    pause_calls = []
    move_calls = []
    tag_calls = []
    original_stop_event = main_module.stop_event
    original_get_qb_session = main_module.get_qb_session
    original_get_torrents = main_module.qb_api.get_torrents
    original_set_share_limits = main_module.qb_api.set_share_limits
    original_add_tags = main_module.qb_api.add_tags
    original_pause = main_module.qb_api.pause_torrents
    original_set_location = main_module.qb_api.set_torrent_location
    original_interval = main_module.config.GROOM_TASK_INTERVAL
    original_seeding_limit = main_module.db.get_config("seeding_time_limit_min")
    original_schedule_amane_ingest = main_module.schedule_amane_ingest

    main_module.stop_event = FakeStopEvent()
    main_module.get_qb_session = lambda: (fake_session, "http://qb.local")
    # Delivered 只在入库闭环真的被触发时打；这里用桩表示 amane 已配置、线程已起。
    main_module.schedule_amane_ingest = lambda movie_id, title=None: True
    main_module.qb_api.get_torrents = lambda session, base_url, tag=None: [
        {
            "hash": "b" * 40,
            "name": "TST-902 Completed Movie",
            "tags": f"jav,{main_module.SEEDING_LIMIT_TAG},{main_module.DOWNLOAD_RECORDED_TAG}",
            "seeding_time": 90 * 60,
            "state": "uploading",
            "progress": 1,
            "total_size": 1024,
        }
    ]
    main_module.qb_api.set_share_limits = lambda *args, **kwargs: True
    main_module.qb_api.add_tags = lambda session, base_url, hashes, tags: tag_calls.append((hashes, tags)) or True
    main_module.qb_api.pause_torrents = lambda session, base_url, hashes: pause_calls.append(hashes) or True
    main_module.qb_api.set_torrent_location = lambda session, base_url, hashes, location: move_calls.append((hashes, location)) or True
    main_module.config.GROOM_TASK_INTERVAL = 0
    try:
        main_module.db.set_config("seeding_time_limit_min", 60)
        main_module.groom_torrents_task()
    finally:
        main_module.db.set_config("seeding_time_limit_min", original_seeding_limit if original_seeding_limit is not None else main_module.config.SEEDING_TIME_LIMIT)
        main_module.stop_event = original_stop_event
        main_module.get_qb_session = original_get_qb_session
        main_module.qb_api.get_torrents = original_get_torrents
        main_module.qb_api.set_share_limits = original_set_share_limits
        main_module.qb_api.add_tags = original_add_tags
        main_module.qb_api.pause_torrents = original_pause
        main_module.qb_api.set_torrent_location = original_set_location
        main_module.config.GROOM_TASK_INTERVAL = original_interval
        main_module.schedule_amane_ingest = original_schedule_amane_ingest

    assert pause_calls == [["b" * 40]]
    assert move_calls == [(["b" * 40], main_module.get_qb_delivery_folder())]
    assert tag_calls == [(["b" * 40], "Delivered")]


def _run_groom_with_torrent(main_module, torrent, seeding_limit=60):
    """用给定的单个种子跑一次 groom_torrents_task，返回 (pause, move, tag) 调用记录。"""
    class FakeStopEvent:
        def __init__(self):
            self.stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _timeout):
            self.stopped = True
            return True

    fake_session = object()
    pause_calls, move_calls, tag_calls = [], [], []
    saved = (
        main_module.stop_event,
        main_module.get_qb_session,
        main_module.qb_api.get_torrents,
        main_module.qb_api.set_share_limits,
        main_module.qb_api.add_tags,
        main_module.qb_api.pause_torrents,
        main_module.qb_api.set_torrent_location,
        main_module.config.GROOM_TASK_INTERVAL,
        main_module.db.get_config("seeding_time_limit_min"),
    )

    main_module.stop_event = FakeStopEvent()
    main_module.get_qb_session = lambda: (fake_session, "http://qb.local")
    main_module.qb_api.get_torrents = lambda session, base_url, tag=None: [torrent]
    main_module.qb_api.set_share_limits = lambda *args, **kwargs: True
    main_module.qb_api.add_tags = lambda s, b, hashes, tags: tag_calls.append((hashes, tags)) or True
    main_module.qb_api.pause_torrents = lambda s, b, hashes: pause_calls.append(hashes) or True
    main_module.qb_api.set_torrent_location = lambda s, b, hashes, loc: move_calls.append((hashes, loc)) or True
    main_module.config.GROOM_TASK_INTERVAL = 0
    try:
        main_module.db.set_config("seeding_time_limit_min", seeding_limit)
        main_module.groom_torrents_task()
    finally:
        (
            main_module.stop_event,
            main_module.get_qb_session,
            main_module.qb_api.get_torrents,
            main_module.qb_api.set_share_limits,
            main_module.qb_api.add_tags,
            main_module.qb_api.pause_torrents,
            main_module.qb_api.set_torrent_location,
            main_module.config.GROOM_TASK_INTERVAL,
            original_seeding_limit,
        ) = saved
        main_module.db.set_config(
            "seeding_time_limit_min",
            original_seeding_limit if original_seeding_limit is not None
            else main_module.config.SEEDING_TIME_LIMIT,
        )
    return pause_calls, move_calls, tag_calls


def test_groom_torrents_delivers_paused_completed_torrent(main_module):
    """qB 达到做种限制后会把种子转成 pausedUP —— 这类种子必须能投递。

    生产实测（2026-10-10）：6 个已完成 jav 种子全是 pausedUP，且 seeding_time 都是 0
    （qB 暂停后不再累计做种时间）。修复前这两点叠加，导致一个都投递不出去，
    表现就是 external_tasks 恒为 0、入库闭环从未跑通。
    """
    torrent = {
        "hash": "c" * 40,
        "name": "TST-903 Paused Completed Movie",
        "tags": f"jav,{main_module.SEEDING_LIMIT_TAG},{main_module.DOWNLOAD_RECORDED_TAG}",
        "seeding_time": 0,       # 关键：暂停后不累计，做种时限永远判不成立
        "state": "pausedUP",     # 关键：已完成 + 暂停做种
        "progress": 1,
        "total_size": 1024,
    }
    original_schedule_amane_ingest = main_module.schedule_amane_ingest
    # Delivered 只在入库闭环真的被触发时打；这里用桩表示 amane 已配置、线程已起。
    main_module.schedule_amane_ingest = lambda movie_id, title=None: True
    try:
        pause_calls, move_calls, tag_calls = _run_groom_with_torrent(main_module, torrent)
    finally:
        main_module.schedule_amane_ingest = original_schedule_amane_ingest

    # 已经是暂停状态，不该再调一次 pause
    assert pause_calls == []
    assert move_calls == [(["c" * 40], main_module.get_qb_delivery_folder())]
    assert tag_calls == [(["c" * 40], "Delivered")]


def test_groom_torrents_still_skips_paused_download(main_module):
    """pausedDL = 没下完就暂停。白名单放宽后绝不能把这类也放进来投递。"""
    torrent = {
        "hash": "d" * 40,
        "name": "TST-904 Paused Incomplete Movie",
        "tags": f"jav,{main_module.SEEDING_LIMIT_TAG}",
        "seeding_time": 0,
        "state": "pausedDL",
        "progress": 1,           # 人为拉满，确保是被 state 白名单拦下而不是被 progress 拦下
        "total_size": 1024,
    }
    pause_calls, move_calls, tag_calls = _run_groom_with_torrent(main_module, torrent)

    assert move_calls == []
    assert tag_calls == []


def test_groom_delivery_notifies_ingest_after_move(main_module):
    """入库通知必须排在投递之后。

    amane 的库根（/hp）不含下载目录（/downloads），文件还在下载目录时就触发
    入库，amane 扫描必然 not_found。所以只有投递（set_torrent_location）成功
    之后才通知；另外已打过 DOWNLOAD_RECORDED_TAG 的种子也必须通知，否则
    投递后没人接手。
    """
    class FakeStopEvent:
        def __init__(self):
            self.stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _timeout):
            self.stopped = True
            return True

    fake_session = object()
    notified = []
    order = []
    service = main_module.get_download_service()
    original_hook = service.on_download_completed
    original_stop_event = main_module.stop_event
    original_get_qb_session = main_module.get_qb_session
    original_get_torrents = main_module.qb_api.get_torrents
    original_set_share_limits = main_module.qb_api.set_share_limits
    original_add_tags = main_module.qb_api.add_tags
    original_pause = main_module.qb_api.pause_torrents
    original_set_location = main_module.qb_api.set_torrent_location
    original_interval = main_module.config.GROOM_TASK_INTERVAL
    original_seeding_limit = main_module.db.get_config("seeding_time_limit_min")

    main_module.stop_event = FakeStopEvent()
    main_module.get_qb_session = lambda: (fake_session, "http://qb.local")
    main_module.qb_api.get_torrents = lambda session, base_url, tag=None: [
        {
            "hash": "c" * 40,
            "name": "TST-903 Delivered Movie",
            "tags": f"jav,{main_module.SEEDING_LIMIT_TAG},{main_module.DOWNLOAD_RECORDED_TAG}",
            "seeding_time": 90 * 60,
            "state": "uploading",
            "progress": 1,
            "total_size": 1024,
        }
    ]
    service.on_download_completed = lambda movie_id, torrent: notified.append(movie_id)
    main_module.qb_api.set_share_limits = lambda *args, **kwargs: True
    main_module.qb_api.add_tags = lambda session, base_url, hashes, tags: True
    main_module.qb_api.pause_torrents = lambda session, base_url, hashes: True
    main_module.qb_api.set_torrent_location = (
        lambda session, base_url, hashes, location: order.append(("move", len(notified))) or True
    )
    main_module.config.GROOM_TASK_INTERVAL = 0
    try:
        main_module.db.set_config("seeding_time_limit_min", 60)
        main_module.groom_torrents_task()
    finally:
        service.on_download_completed = original_hook
        main_module.db.set_config("seeding_time_limit_min", original_seeding_limit if original_seeding_limit is not None else main_module.config.SEEDING_TIME_LIMIT)
        main_module.stop_event = original_stop_event
        main_module.get_qb_session = original_get_qb_session
        main_module.qb_api.get_torrents = original_get_torrents
        main_module.qb_api.set_share_limits = original_set_share_limits
        main_module.qb_api.add_tags = original_add_tags
        main_module.qb_api.pause_torrents = original_pause
        main_module.qb_api.set_torrent_location = original_set_location
        main_module.config.GROOM_TASK_INTERVAL = original_interval

    assert order == [("move", 0)]  # 移动发生时还没通知过
    assert notified == ["TST-903"]


def test_groom_notifies_ingest_even_without_delivery_folder(main_module):
    """没配投递目录时也要通知入库，否则片子永远进不了 amane。"""
    class FakeStopEvent:
        def __init__(self):
            self.stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _timeout):
            self.stopped = True
            return True

    fake_session = object()
    notified = []
    move_calls = []
    service = main_module.get_download_service()
    original_hook = service.on_download_completed
    original_folder = service.get_qb_delivery_folder
    original_stop_event = main_module.stop_event
    original_get_qb_session = main_module.get_qb_session
    original_get_torrents = main_module.qb_api.get_torrents
    original_set_share_limits = main_module.qb_api.set_share_limits
    original_add_tags = main_module.qb_api.add_tags
    original_pause = main_module.qb_api.pause_torrents
    original_set_location = main_module.qb_api.set_torrent_location
    original_interval = main_module.config.GROOM_TASK_INTERVAL
    original_seeding_limit = main_module.db.get_config("seeding_time_limit_min")

    main_module.stop_event = FakeStopEvent()
    main_module.get_qb_session = lambda: (fake_session, "http://qb.local")
    main_module.qb_api.get_torrents = lambda session, base_url, tag=None: [
        {
            "hash": "d" * 40,
            "name": "TST-904 No Delivery Folder",
            "tags": f"jav,{main_module.SEEDING_LIMIT_TAG},{main_module.DOWNLOAD_RECORDED_TAG}",
            "seeding_time": 90 * 60,
            "state": "uploading",
            "progress": 1,
            "total_size": 1024,
        }
    ]
    service.on_download_completed = lambda movie_id, torrent: notified.append(movie_id)
    service.get_qb_delivery_folder = lambda: None
    main_module.qb_api.set_share_limits = lambda *args, **kwargs: True
    main_module.qb_api.add_tags = lambda session, base_url, hashes, tags: True
    main_module.qb_api.pause_torrents = lambda session, base_url, hashes: True
    main_module.qb_api.set_torrent_location = (
        lambda session, base_url, hashes, location: move_calls.append(location) or True
    )
    main_module.config.GROOM_TASK_INTERVAL = 0
    try:
        main_module.db.set_config("seeding_time_limit_min", 60)
        main_module.groom_torrents_task()
    finally:
        service.on_download_completed = original_hook
        service.get_qb_delivery_folder = original_folder
        main_module.db.set_config("seeding_time_limit_min", original_seeding_limit if original_seeding_limit is not None else main_module.config.SEEDING_TIME_LIMIT)
        main_module.stop_event = original_stop_event
        main_module.get_qb_session = original_get_qb_session
        main_module.qb_api.get_torrents = original_get_torrents
        main_module.qb_api.set_share_limits = original_set_share_limits
        main_module.qb_api.add_tags = original_add_tags
        main_module.qb_api.pause_torrents = original_pause
        main_module.qb_api.set_torrent_location = original_set_location
        main_module.config.GROOM_TASK_INTERVAL = original_interval

    assert move_calls == []  # 目录无效，不做投递
    assert notified == ["TST-904"]


def test_groom_skips_delivery_for_torrents_without_movie_id(main_module):
    """解析不出番号的种子不投递。

    投递目录现在落在 amane 库内（/hp/_incoming），刷流 / 电影 / 剧集这类共用同一个
    qB 的种子搬进去会被登记又刮削失败，堆成待人工垃圾。
    """
    class FakeStopEvent:
        def __init__(self):
            self.stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _timeout):
            self.stopped = True
            return True

    fake_session = object()
    notified = []
    move_calls = []
    pause_calls = []
    service = main_module.get_download_service()
    original_hook = service.on_download_completed
    original_stop_event = main_module.stop_event
    original_get_qb_session = main_module.get_qb_session
    original_get_torrents = main_module.qb_api.get_torrents
    original_set_share_limits = main_module.qb_api.set_share_limits
    original_add_tags = main_module.qb_api.add_tags
    original_pause = main_module.qb_api.pause_torrents
    original_set_location = main_module.qb_api.set_torrent_location
    original_interval = main_module.config.GROOM_TASK_INTERVAL
    original_seeding_limit = main_module.db.get_config("seeding_time_limit_min")

    main_module.stop_event = FakeStopEvent()
    main_module.get_qb_session = lambda: (fake_session, "http://qb.local")
    # 名字里没有番号，extract_movie_id_from_text 返回 None
    main_module.qb_api.get_torrents = lambda session, base_url, tag=None: [
        {
            "hash": "e" * 40,
            "name": "BT-Drill-No-Number.mkv",
            "tags": f"jav,{main_module.SEEDING_LIMIT_TAG},{main_module.DOWNLOAD_RECORDED_TAG}",
            "seeding_time": 90 * 60,
            "state": "uploading",
            "progress": 1,
            "total_size": 1024,
        }
    ]
    service.on_download_completed = lambda movie_id, torrent: notified.append(movie_id)
    main_module.qb_api.set_share_limits = lambda *args, **kwargs: True
    main_module.qb_api.add_tags = lambda session, base_url, hashes, tags: True
    main_module.qb_api.pause_torrents = lambda session, base_url, hashes: pause_calls.append(hashes) or True
    main_module.qb_api.set_torrent_location = (
        lambda session, base_url, hashes, location: move_calls.append(location) or True
    )
    main_module.config.GROOM_TASK_INTERVAL = 0
    try:
        main_module.db.set_config("seeding_time_limit_min", 60)
        main_module.groom_torrents_task()
    finally:
        service.on_download_completed = original_hook
        main_module.db.set_config("seeding_time_limit_min", original_seeding_limit if original_seeding_limit is not None else main_module.config.SEEDING_TIME_LIMIT)
        main_module.stop_event = original_stop_event
        main_module.get_qb_session = original_get_qb_session
        main_module.qb_api.get_torrents = original_get_torrents
        main_module.qb_api.set_share_limits = original_set_share_limits
        main_module.qb_api.add_tags = original_add_tags
        main_module.qb_api.pause_torrents = original_pause
        main_module.qb_api.set_torrent_location = original_set_location
        main_module.config.GROOM_TASK_INTERVAL = original_interval

    assert move_calls == []
    assert pause_calls == []
    assert notified == []


def test_groom_torrents_tolerates_dirty_qb_torrent_fields(main_module):
    class FakeStopEvent:
        def __init__(self):
            self.stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _timeout):
            self.stopped = True
            return True

    fake_session = object()
    share_limit_calls = []
    tag_calls = []
    filter_calls = []
    original_stop_event = main_module.stop_event
    original_get_qb_session = main_module.get_qb_session
    original_get_torrents = main_module.qb_api.get_torrents
    original_set_share_limits = main_module.qb_api.set_share_limits
    original_add_tags = main_module.qb_api.add_tags
    original_filter = main_module.filter_jav_torrent_files
    original_interval = main_module.config.GROOM_TASK_INTERVAL

    main_module.stop_event = FakeStopEvent()
    main_module.get_qb_session = lambda: (fake_session, "http://qb.local")
    main_module.qb_api.get_torrents = lambda session, base_url, tag=None: [
        "not-a-torrent",
        {"hash": "", "name": "Missing Hash", "tags": ["JAV"], "seeding_time": "dirty", "state": None, "progress": "0.2", "total_size": 1024},
        {"hash": "dirty-tags-hash", "name": "Dirty Tags", "tags": ["JAV", None, " "], "seeding_time": "dirty", "state": None, "progress": "0.2", "total_size": 1024},
    ]

    def fake_set_share_limits(session, base_url, hashes, ratio_limit, seeding_time_limit):
        share_limit_calls.append((hashes, ratio_limit, seeding_time_limit))
        return True

    def fake_add_tags(session, base_url, hashes, tags):
        tag_calls.append((hashes, tags))
        return True

    def fake_filter(session, base_url, torrent):
        filter_calls.append(torrent.get("hash"))
        return {"filtered": False, "reason": "scenario"}

    main_module.qb_api.set_share_limits = fake_set_share_limits
    main_module.qb_api.add_tags = fake_add_tags
    main_module.filter_jav_torrent_files = fake_filter
    main_module.config.GROOM_TASK_INTERVAL = 0
    try:
        main_module.groom_torrents_task()
    finally:
        main_module.stop_event = original_stop_event
        main_module.get_qb_session = original_get_qb_session
        main_module.qb_api.get_torrents = original_get_torrents
        main_module.qb_api.set_share_limits = original_set_share_limits
        main_module.qb_api.add_tags = original_add_tags
        main_module.filter_jav_torrent_files = original_filter
        main_module.config.GROOM_TASK_INTERVAL = original_interval

    assert share_limit_calls == [(["dirty-tags-hash"], -2, main_module.config.SEEDING_TIME_LIMIT)]
    assert tag_calls == [(["dirty-tags-hash"], main_module.SEEDING_LIMIT_TAG)]
    assert filter_calls == ["dirty-tags-hash"]


def test_runtime_numeric_config_tolerates_dirty_values(main_module):
    fake_session = object()
    retention_calls = []
    share_limit_calls = []

    original_smtp_port = main_module.db.get_config("smtp_port")
    original_notify_enabled = main_module.db.get_config("notify_enabled")
    original_email_enabled = main_module.db.get_config("email_enabled")
    original_retention_days = main_module.db.get_config("retention_days")
    original_seeding_limit = main_module.db.get_config("seeding_time_limit_min")
    original_get_expired = main_module.db.get_lifecycle_expired_movies
    original_stop_event = main_module.stop_event
    original_get_qb_session = main_module.get_qb_session
    original_get_torrents = main_module.qb_api.get_torrents
    original_set_share_limits = main_module.qb_api.set_share_limits
    original_add_tags = main_module.qb_api.add_tags
    original_interval = main_module.config.GROOM_TASK_INTERVAL

    class FakeStopEvent:
        def __init__(self):
            self.stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _timeout):
            self.stopped = True
            return True

    def fake_get_expired(retention_days):
        retention_calls.append(retention_days)
        return []

    def fake_set_share_limits(session, base_url, hashes, ratio_limit, seeding_time_limit):
        share_limit_calls.append((hashes, ratio_limit, seeding_time_limit))
        return True

    try:
        main_module.db.set_config("smtp_port", "dirty-port")
        main_module.db.set_config("notify_enabled", "yes")
        main_module.db.set_config("email_enabled", None)
        main_module.notifier.notifier.reload_config()
        assert main_module.notifier.notifier.smtp_port == 465
        assert main_module.notifier.notifier.enabled is True

        main_module.db.set_config("retention_days", "dirty-days")
        main_module.db.get_lifecycle_expired_movies = fake_get_expired
        main_module.lifecycle_manager.lifecycle_manager.run_auto_delete_gc()
        assert retention_calls == [30]

        main_module.db.set_config("seeding_time_limit_min", "dirty-limit")
        main_module.stop_event = FakeStopEvent()
        main_module.get_qb_session = lambda: (fake_session, "http://qb.local")
        main_module.qb_api.get_torrents = lambda session, base_url, tag=None: [
            {"hash": "abc123", "name": "Dirty Limit Torrent", "tags": "jav", "seeding_time": 0, "state": "uploading"}
        ]
        main_module.qb_api.set_share_limits = fake_set_share_limits
        main_module.qb_api.add_tags = lambda *args, **kwargs: True
        main_module.config.GROOM_TASK_INTERVAL = 0
        main_module.groom_torrents_task()
        assert share_limit_calls == [(["abc123"], -2, main_module.config.SEEDING_TIME_LIMIT)]
    finally:
        main_module.db.set_config("smtp_port", original_smtp_port if original_smtp_port is not None else 465)
        main_module.db.set_config("notify_enabled", original_notify_enabled if original_notify_enabled is not None else "false")
        main_module.db.set_config("email_enabled", original_email_enabled)
        main_module.db.set_config("retention_days", original_retention_days if original_retention_days is not None else 30)
        main_module.db.set_config("seeding_time_limit_min", original_seeding_limit if original_seeding_limit is not None else main_module.config.SEEDING_TIME_LIMIT)
        main_module.db.get_lifecycle_expired_movies = original_get_expired
        main_module.stop_event = original_stop_event
        main_module.get_qb_session = original_get_qb_session
        main_module.qb_api.get_torrents = original_get_torrents
        main_module.qb_api.set_share_limits = original_set_share_limits
        main_module.qb_api.add_tags = original_add_tags
        main_module.config.GROOM_TASK_INTERVAL = original_interval
        main_module.notifier.notifier.reload_config()


def test_notifier_uses_system_config_email_keys(main_module):
    keys = ["email_enabled", "email_host", "email_port", "email_user", "email_pass", "email_to"]
    original = {key: main_module.db.get_config(key) for key in keys}
    try:
        main_module.db.set_config("email_enabled", True)
        main_module.db.set_config("email_host", "smtp.example.test")
        main_module.db.set_config("email_port", 587)
        main_module.db.set_config("email_user", "sender@example.test")
        main_module.db.set_config("email_pass", "secret")
        main_module.db.set_config("email_to", "receiver@example.test")

        main_module.notifier.notifier.reload_config()

        assert main_module.notifier.notifier.enabled is True
        assert main_module.notifier.notifier.smtp_host == "smtp.example.test"
        assert main_module.notifier.notifier.smtp_port == 587
        assert main_module.notifier.notifier.smtp_user == "sender@example.test"
        assert main_module.notifier.notifier.smtp_pass == "secret"
        assert main_module.notifier.notifier.receiver == "receiver@example.test"
    finally:
        for key, value in original.items():
            main_module.db.set_config(key, value)
        main_module.notifier.notifier.reload_config()


def test_jav_file_filter_tags_small_file_only_torrent(main_module):
    fake_session = object()
    priority_calls = []
    tag_calls = []
    original_get_files = main_module.qb_api.get_torrent_files
    original_set_priority = main_module.qb_api.set_file_priority
    original_add_tags = main_module.qb_api.add_tags

    main_module.qb_api.get_torrent_files = lambda session, base_url, torrent_hash: [
        {"index": 0, "name": "ad.txt", "size": 1024},
        {"index": 1, "name": "sample.mp4", "size": 100 * 1024 * 1024},
    ]

    def fake_set_priority(session, base_url, torrent_hash, file_ids, priority):
        priority_calls.append((torrent_hash, file_ids, priority))
        return True

    def fake_add_tags(session, base_url, hashes, tags):
        tag_calls.append((hashes, tags))
        return True

    main_module.qb_api.set_file_priority = fake_set_priority
    main_module.qb_api.add_tags = fake_add_tags
    try:
        result = main_module.filter_jav_torrent_files(
            fake_session,
            "http://qb.local",
            {"hash": "a" * 40, "name": "Small Files", "progress": 0.2, "total_size": 100},
        )
    finally:
        main_module.qb_api.get_torrent_files = original_get_files
        main_module.qb_api.set_file_priority = original_set_priority
        main_module.qb_api.add_tags = original_add_tags

    assert result["filtered"] is False
    assert result["reason"] == "no_video_over_400mb"
    assert result["small_files_disabled"] == 2
    assert result["disabled_files"] == 2
    assert priority_calls == [("a" * 40, [0, 1], 0)]
    assert tag_calls == [(["a" * 40], main_module.JAV_FILE_FILTER_TAG)]


def test_jav_file_filter_reports_priority_failure(main_module):
    original_get_files = main_module.qb_api.get_torrent_files
    original_set_priority = main_module.qb_api.set_file_priority
    original_add_tags = main_module.qb_api.add_tags
    main_module.qb_api.get_torrent_files = lambda *_args: [
        {"index": 0, "name": "main.mkv", "size": 800 * 1024 * 1024},
    ]
    main_module.qb_api.set_file_priority = lambda *_args: False
    main_module.qb_api.add_tags = lambda *_args: (_ for _ in ()).throw(AssertionError("tag update called"))
    try:
        result = main_module.filter_jav_torrent_files(
            object(),
            "http://qb.local",
            {"hash": "d" * 40, "progress": 0.2, "total_size": 800 * 1024 * 1024},
        )
    finally:
        main_module.qb_api.get_torrent_files = original_get_files
        main_module.qb_api.set_file_priority = original_set_priority
        main_module.qb_api.add_tags = original_add_tags

    assert result == {"filtered": False, "reason": "file_priority_failed"}


def test_jav_file_filter_disables_large_non_video_when_no_valid_video(main_module):
    fake_session = object()
    priority_calls = []
    tag_calls = []
    original_get_files = main_module.qb_api.get_torrent_files
    original_set_priority = main_module.qb_api.set_file_priority
    original_add_tags = main_module.qb_api.add_tags

    main_module.qb_api.get_torrent_files = lambda session, base_url, torrent_hash: [
        {"index": 0, "name": "ad-bundle.zip", "size": 900 * 1024 * 1024},
        {"index": 1, "name": "sample.mp4", "size": 120 * 1024 * 1024},
    ]

    def fake_set_priority(session, base_url, torrent_hash, file_ids, priority):
        priority_calls.append((torrent_hash, file_ids, priority))
        return True

    def fake_add_tags(session, base_url, hashes, tags):
        tag_calls.append((hashes, tags))
        return True

    main_module.qb_api.set_file_priority = fake_set_priority
    main_module.qb_api.add_tags = fake_add_tags
    try:
        result = main_module.filter_jav_torrent_files(
            fake_session,
            "http://qb.local",
            {"hash": "c" * 40, "name": "Large Ads", "progress": 0.2, "total_size": 100},
        )
    finally:
        main_module.qb_api.get_torrent_files = original_get_files
        main_module.qb_api.set_file_priority = original_set_priority
        main_module.qb_api.add_tags = original_add_tags

    assert result["filtered"] is False
    assert result["reason"] == "no_video_over_400mb"
    assert result["small_files_disabled"] == 1
    assert result["disabled_files"] == 2
    assert priority_calls == [("c" * 40, [0, 1], 0)]
    assert tag_calls == [(["c" * 40], main_module.JAV_FILE_FILTER_TAG)]


def test_jav_file_filter_tolerates_dirty_qb_file_entries(main_module):
    fake_session = object()
    priority_calls = []
    tag_calls = []
    original_get_files = main_module.qb_api.get_torrent_files
    original_set_priority = main_module.qb_api.set_file_priority
    original_add_tags = main_module.qb_api.add_tags

    main_module.qb_api.get_torrent_files = lambda session, base_url, torrent_hash: [
        "not-a-file",
        {"index": "bad-index", "name": "broken.mp4", "size": "dirty"},
        {"index": 2, "name": "main.mkv", "size": 800 * 1024 * 1024},
    ]

    def fake_set_priority(session, base_url, torrent_hash, file_ids, priority):
        priority_calls.append((torrent_hash, file_ids, priority))
        return True

    def fake_add_tags(session, base_url, hashes, tags):
        tag_calls.append((hashes, tags))
        return True

    main_module.qb_api.set_file_priority = fake_set_priority
    main_module.qb_api.add_tags = fake_add_tags
    try:
        assert main_module.is_groomable_torrent("not-a-torrent") is False
        assert main_module.is_groomable_torrent({"progress": "dirty", "total_size": "1024"}) is True
        result = main_module.filter_jav_torrent_files(
            fake_session,
            "http://qb.local",
            {"hash": "b" * 40, "name": "Dirty Files", "progress": "dirty", "total_size": "dirty"},
        )
    finally:
        main_module.qb_api.get_torrent_files = original_get_files
        main_module.qb_api.set_file_priority = original_set_priority
        main_module.qb_api.add_tags = original_add_tags

    assert result["filtered"] is True
    assert result["kept_file"] == "main.mkv"
    assert priority_calls == [("b" * 40, [1], 0), ("b" * 40, [2], 1)]
    assert tag_calls == [(["b" * 40], main_module.JAV_FILE_FILTER_TAG)]


def test_lifecycle_pre_download_ad_cleaner_tolerates_dirty_qb_file_entries(main_module):
    fake_session = object()
    priority_calls = []
    info_calls = []
    original_get_files = main_module.lifecycle_manager.qb_api.get_torrent_files
    original_set_priority = main_module.lifecycle_manager.qb_api.set_file_priority
    original_info = main_module.lifecycle_manager.logger.info

    main_module.lifecycle_manager.qb_api.get_torrent_files = lambda session, base_url, torrent_hash: [
        "not-a-file",
        {"index": "bad-index", "name": "广告.url"},
        {"name": ""},
        {"index": 3, "name": "main.mkv"},
    ]

    def fake_set_priority(session, base_url, torrent_hash, file_ids, priority):
        priority_calls.append((torrent_hash, file_ids, priority))
        return True

    main_module.lifecycle_manager.qb_api.set_file_priority = fake_set_priority
    main_module.lifecycle_manager.logger.info = lambda *args, **kwargs: info_calls.append((args, kwargs))
    try:
        main_module.lifecycle_manager.lifecycle_manager.clean_ads_pre_download(
            fake_session,
            "http://qb.local",
            "dirty-pre-clean",
        )
    finally:
        main_module.lifecycle_manager.qb_api.get_torrent_files = original_get_files
        main_module.lifecycle_manager.qb_api.set_file_priority = original_set_priority
        main_module.lifecycle_manager.logger.info = original_info

    assert priority_calls == [("dirty-pre-clean", [1], 0)]
    assert info_calls
    assert all("dirty-pre-clean" not in str(call) for call in info_calls)


def test_lifecycle_gc_creates_candidate_without_deleting(main_module, temp_dir):
    info_calls = []
    candidate_calls = []
    original_get_expired = main_module.db.get_lifecycle_expired_movies
    original_upsert_candidate = main_module.db.upsert_cleanup_candidate
    original_safe_remove = main_module.lifecycle_manager.LifecycleManager._safe_remove_managed_file
    original_info = main_module.lifecycle_manager.logger.info
    movie_path = Path(temp_dir.name) / "private-title.mp4"
    movie_path.write_text("movie", encoding="utf-8")

    main_module.db.get_lifecycle_expired_movies = lambda retention_days: [
        {
            "id": "SECRET-999",
            "title": "Very Private Movie Title",
            "file_path": str(movie_path),
        }
    ]
    main_module.db.upsert_cleanup_candidate = lambda **kwargs: candidate_calls.append(kwargs) or "candidate-id"
    main_module.lifecycle_manager.LifecycleManager._safe_remove_managed_file = staticmethod(
        lambda path, description: (_ for _ in ()).throw(AssertionError("GC must not delete files"))
    )
    main_module.lifecycle_manager.logger.info = lambda *args, **kwargs: info_calls.append((args, kwargs))
    try:
        result = main_module.lifecycle_manager.lifecycle_manager.run_auto_delete_gc()
    finally:
        main_module.db.get_lifecycle_expired_movies = original_get_expired
        main_module.db.upsert_cleanup_candidate = original_upsert_candidate
        main_module.lifecycle_manager.LifecycleManager._safe_remove_managed_file = original_safe_remove
        main_module.lifecycle_manager.logger.info = original_info

    assert result == {"scanned": 1, "candidates": 1}
    assert candidate_calls == [
        {
            "jellyfin_id": "local:SECRET-999",
            "movie_id": "SECRET-999",
            "title": "Very Private Movie Title",
            "file_path": str(movie_path),
            "reason": "retention_expired:30d",
            "source": "lifecycle_retention_scan",
        }
    ]
    assert movie_path.exists()
    serialized = str(info_calls)
    assert "SECRET-999" not in serialized
    assert "Very Private Movie Title" not in serialized


def test_lifecycle_auto_delete_uses_exact_path_matching(main_module, temp_dir):
    original_get_targets = main_module.db.get_auto_delete_targets
    original_get_magnet = main_module.db.get_magnet_for_movie
    original_get_torrents = main_module.qb_api.get_torrents
    original_delete_torrents = main_module.qb_api.delete_torrents
    original_safe_remove = main_module.lifecycle_manager.LifecycleManager._safe_remove_managed_file

    file_path = Path(temp_dir.name) / "ABC-123" / "movie.mp4"
    symlink_path = Path(temp_dir.name) / "links" / "ABC-123-link.mp4"
    near_miss_path = Path(temp_dir.name) / "ABC-1234" / "movie.mp4"

    main_module.db.get_auto_delete_targets = lambda **kwargs: [
        {
            "id": "MOV-001",
            "title": "Exact Match Movie",
            "jellyfin_id": "jf-001",
            "file_path": str(file_path),
            "symlink_path": str(symlink_path),
        }
    ]
    main_module.db.get_magnet_for_movie = lambda movie_id: None
    delete_calls = []
    main_module.qb_api.get_torrents = lambda session, base_url, tag=None: [
        {"hash": "exact-file", "content_path": str(file_path)},
        {"hash": "substring-miss", "content_path": str(near_miss_path)},
        {"hash": "exact-link", "save_path": str(symlink_path)},
    ]
    main_module.qb_api.delete_torrents = lambda session, base_url, hashes, delete_files=False: delete_calls.append(list(hashes)) or True
    main_module.lifecycle_manager.LifecycleManager._safe_remove_managed_file = staticmethod(lambda path, description: False)

    try:
        result = main_module.lifecycle_manager.LifecycleManager.run_auto_delete_watched(
            object(),
            "http://qb.local",
            lambda jellyfin_id: True,
        )
    finally:
        main_module.db.get_auto_delete_targets = original_get_targets
        main_module.db.get_magnet_for_movie = original_get_magnet
        main_module.qb_api.get_torrents = original_get_torrents
        main_module.qb_api.delete_torrents = original_delete_torrents
        main_module.lifecycle_manager.LifecycleManager._safe_remove_managed_file = original_safe_remove

    assert result["deleted_torrents"] == 1
    assert delete_calls == [["exact-file", "exact-link"]]


def test_auto_delete_watched_logs_audit_write_failure(main_module):
    original_get_targets = main_module.db.get_auto_delete_targets
    original_get_magnet = main_module.db.get_magnet_for_movie
    original_get_torrents = main_module.qb_api.get_torrents
    original_delete_torrents = main_module.qb_api.delete_torrents
    original_safe_remove = main_module.lifecycle_manager.LifecycleManager._safe_remove_managed_file
    original_record_event = main_module.db.record_automation_event
    original_warning = main_module.lifecycle_manager.logger.warning
    warning_calls = []

    main_module.db.get_auto_delete_targets = lambda **kwargs: [
        {
            "id": "MOV-001",
            "title": "Audit Failure Movie",
            "jellyfin_id": "jf-001",
            "file_path": "C:/tmp/audit/movie.mp4",
            "symlink_path": "C:/tmp/audit/link.mp4",
        }
    ]
    main_module.db.get_magnet_for_movie = lambda movie_id: None
    main_module.qb_api.get_torrents = lambda session, base_url, tag=None: []
    main_module.qb_api.delete_torrents = lambda session, base_url, hashes, delete_files=False: True
    main_module.lifecycle_manager.LifecycleManager._safe_remove_managed_file = staticmethod(lambda path, description: False)

    def raise_audit_error(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    def capture_warning(*args, **kwargs):
        warning_calls.append((args, kwargs))

    main_module.db.record_automation_event = raise_audit_error
    main_module.lifecycle_manager.logger.warning = capture_warning
    try:
        result = main_module.lifecycle_manager.LifecycleManager.run_auto_delete_watched(
            object(),
            "http://qb.local",
            lambda jellyfin_id: True,
        )
    finally:
        main_module.db.get_auto_delete_targets = original_get_targets
        main_module.db.get_magnet_for_movie = original_get_magnet
        main_module.qb_api.get_torrents = original_get_torrents
        main_module.qb_api.delete_torrents = original_delete_torrents
        main_module.lifecycle_manager.LifecycleManager._safe_remove_managed_file = original_safe_remove
        main_module.db.record_automation_event = original_record_event
        main_module.lifecycle_manager.logger.warning = original_warning

    assert result["items"][0]["jellyfin"] == "deleted"
    assert result["items"][0]["torrent"] == "not_found"
    assert len(warning_calls) == 1
    args, _kwargs = warning_calls[0]
    assert args[0] == "Auto-delete: failed to record automation audit for %s (reason=%s): %s"
    assert args[2] == "auto_delete"
    assert args[3] == "RuntimeError"


def test_get_recent_movies_fallback_uses_parameterized_query(main_module):
    original_get_connection = main_module.db.get_connection
    original_release_connection = main_module.db._release_connection
    original_is_postgres = main_module.db.is_postgres

    class FakeCursor:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params):
            self.calls.append((sql, params))

        def fetchall(self):
            return []

    class FakeConnection:
        def __init__(self):
            self.cursor_obj = FakeCursor()

        def cursor(self):
            return self.cursor_obj

    fake_conn = FakeConnection()
    main_module.db.get_connection = lambda: fake_conn
    main_module.db._release_connection = lambda conn: None

    try:
        main_module.db.is_postgres = False
        assert main_module.db.get_recent_movies_fallback(limit=2, offset=5) == []
        sql, params = fake_conn.cursor_obj.calls[-1]
        assert "LIMIT ? OFFSET ?" in sql
        assert params == (2, 5)

        fake_conn.cursor_obj.calls.clear()
        main_module.db.is_postgres = True
        assert main_module.db.get_recent_movies_fallback(limit=3, offset=7) == []
        sql, params = fake_conn.cursor_obj.calls[-1]
        assert "LIMIT %s OFFSET %s" in sql
        assert params == (3, 7)
    finally:
        main_module.db.get_connection = original_get_connection
        main_module.db._release_connection = original_release_connection
        main_module.db.is_postgres = original_is_postgres


def test_cache_size_limit_falls_back_for_dirty_config(main_module):
    main_module.db.set_config("cache_size_limit_gb", "dirty")
    assert main_module.get_cache_size_limit_gb() == 10.0

    main_module.db.set_config("cache_size_limit_gb", 0)
    assert main_module.get_cache_size_limit_gb() == 10.0

    main_module.db.set_config("cache_size_limit_gb", -5)
    assert main_module.get_cache_size_limit_gb() == 10.0

    main_module.db.set_config("cache_size_limit_gb", 0.5)
    assert main_module.get_cache_size_limit_gb() == 0.5

    main_module.db.set_config("cache_size_limit_gb", 1001)
    assert main_module.get_cache_size_limit_gb() == main_module.config.CACHE_SIZE_LIMIT_GB


def test_cache_size_limit_rejects_non_finite_values(main_module):
    assert "cache_size_limit_gb_invalid_number" in main_module.validate_system_config_payload(
        {"cache_size_limit_gb": float("nan")}
    )
    assert "cache_size_limit_gb_invalid_number" in main_module.validate_system_config_payload(
        {"cache_size_limit_gb": float("inf")}
    )


def test_cache_cleanup_only_deletes_safe_image_cache_files(main_module, temp_dir):
    original_cache_dir = main_module.COVER_CACHE_DIR
    original_get_limit = main_module.get_cache_size_limit_gb
    cache_root = Path(temp_dir.name) / "cleanup-cache"
    proxy_dir = cache_root / "proxy"
    proxy_dir.mkdir(parents=True, exist_ok=True)

    old_image = proxy_dir / "old.jpg"
    old_image.write_bytes(b"old1")
    new_image = proxy_dir / "new.jpg"
    new_image.write_bytes(b"new1")
    ignored_text = proxy_dir / "keep.txt"
    ignored_text.write_bytes(b"not-image-cache")
    ignored_directory = proxy_dir / "fake.jpg"
    ignored_directory.mkdir()

    now = time.time()
    os.utime(old_image, (now - 20, now - 20))
    os.utime(new_image, (now - 10, now - 10))

    main_module.COVER_CACHE_DIR = str(cache_root)
    main_module.get_cache_size_limit_gb = lambda: 5 / (1024 ** 3)
    try:
        summary = main_module.run_cache_cleanup_once()
    finally:
        main_module.COVER_CACHE_DIR = original_cache_dir
        main_module.get_cache_size_limit_gb = original_get_limit

    assert summary["deleted_count"] == 1
    assert not old_image.exists()
    assert new_image.exists()
    assert ignored_text.exists()
    assert ignored_directory.is_dir()


def test_cache_cleanup_uses_high_and_low_watermarks(main_module, temp_dir):
    original_cache_dir = main_module.COVER_CACHE_DIR
    original_get_limit = main_module.get_cache_size_limit_gb
    cache_root = Path(temp_dir.name) / "watermark-cache"
    proxy_dir = cache_root / "proxy"
    proxy_dir.mkdir(parents=True, exist_ok=True)

    files = []
    now = time.time()
    for index in range(3):
        path = proxy_dir / f"{index}.jpg"
        path.write_bytes(b"1234")
        os.utime(path, (now - 30 + index, now - 30 + index))
        files.append(path)

    main_module.COVER_CACHE_DIR = str(cache_root)
    main_module.get_cache_size_limit_gb = lambda: 10 / (1024 ** 3)
    try:
        summary = main_module.run_cache_cleanup_once()
        assert summary["status"] == "cleaned"
        assert summary["deleted_count"] == 1
        assert summary["cache_size_gb"] <= 8 / (1024 ** 3)
        assert not files[0].exists()

        second_summary = main_module.run_cache_cleanup_once()
        assert second_summary["status"] == "within_limit"
        assert second_summary["deleted_count"] == 0
    finally:
        main_module.COVER_CACHE_DIR = original_cache_dir
        main_module.get_cache_size_limit_gb = original_get_limit


def test_image_cache_atomic_write_and_stats(main_module, temp_dir):
    from image_cache_service import ImageCacheService

    cache_root = Path(temp_dir.name) / "atomic-cache"
    service = ImageCacheService(str(cache_root))
    target = service.proxy_dir / "poster.jpg"

    assert service.write_cache_bytes_if_safe(target, service.proxy_dir, b"poster") is True
    assert target.read_bytes() == b"poster"
    assert not list(service.proxy_dir.glob("*.tmp"))

    jellyfin_dir = cache_root / "jellyfin"
    jellyfin_dir.mkdir()
    (jellyfin_dir / "cover.jpg").write_bytes(b"jf")
    stats = service.get_stats()
    assert stats["total_count"] == 2
    assert stats["categories"]["remote_images"]["count"] == 1
    assert stats["categories"]["jellyfin_images"]["count"] == 1


def test_image_cache_coalesces_concurrent_fetches(main_module, temp_dir):
    from concurrent.futures import ThreadPoolExecutor
    from image_cache_service import ImageCacheService
    import requests

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "image/jpeg"}

        def iter_content(self, chunk_size=65536):
            time.sleep(0.05)
            yield b"shared-image"

    service = ImageCacheService(str(Path(temp_dir.name) / "coalesced-cache"))
    url = "https://93.184.216.34/shared.jpg"
    original_get = requests.get
    calls = []
    requests.get = lambda *args, **kwargs: calls.append(args[0]) or FakeResponse()
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(service.cache_remote_url, [url, url]))
    finally:
        requests.get = original_get

    assert len(calls) == 1
    assert results[0] == results[1]
    assert Path(results[0]).read_bytes() == b"shared-image"
    assert service.refreshing_count == 0


async def test_cache_stats_api_reports_capacity_and_categories(main_module):
    response = await main_module.get_cache_stats()
    assert response["success"] is True
    data = response["data"]
    assert data["limit_gb"] >= 0.1
    assert data["trigger_ratio"] == 0.9
    assert data["target_ratio"] == 0.8
    assert "remote_images" in data["images"]["categories"]
    assert "translations" in data["structured"]["categories"]


async def test_cache_invalidation_requires_confirmation_for_all(main_module):
    try:
        await main_module.invalidate_cache(
            main_module.CacheInvalidationRequest(category="all")
        )
    except main_module.HTTPException as exc:
        assert exc.status_code == 400
        assert exc.detail == "cache_invalidation_confirmation_required"
    else:
        raise AssertionError("full cache invalidation must require explicit confirmation")


def test_image_cache_invalidation_is_category_scoped(main_module, temp_dir):
    from image_cache_service import ImageCacheService

    cache_root = Path(temp_dir.name) / "invalidate-images"
    service = ImageCacheService(str(cache_root))
    service.proxy_dir.mkdir(parents=True)
    thumb_dir = service.proxy_dir / "thumb_360"
    thumb_dir.mkdir()
    jellyfin_dir = cache_root / "jellyfin"
    jellyfin_dir.mkdir()

    remote = service.proxy_dir / "remote.jpg"
    thumbnail = thumb_dir / "thumb.jpg"
    jellyfin = jellyfin_dir / "jellyfin.jpg"
    ignored = cache_root / "keep.txt"
    for path in (remote, thumbnail, jellyfin, ignored):
        path.write_bytes(b"cache")

    result = service.invalidate_category("remote_images")
    assert result["deleted_count"] == 1
    assert not remote.exists()
    assert thumbnail.exists()
    assert jellyfin.exists()
    assert ignored.exists()


def test_structured_cache_invalidation_is_category_scoped(main_module):
    detail_key = "movie_detail_invalidation_test"
    navigation_key = "navigation_invalidation_test"
    main_module.db.set_cache(detail_key, {"id": "DETAIL-001"})
    main_module.db.set_cache(navigation_key, {"items": ["keep"]})

    result = main_module.db.invalidate_cache_category("movie_details")
    assert result["cache_entries"] >= 1
    assert main_module.db.get_cache(detail_key) is None
    assert main_module.db.get_cache(navigation_key) == {"items": ["keep"]}


def test_cache_warmup_uses_bounded_local_catalog(main_module):
    from fastapi.testclient import TestClient

    original_search_movies = main_module.db.search_movies
    original_get_actresses = main_module.db.get_actresses
    original_schedule = main_module.schedule_image_cache_warmup
    scheduled = []

    main_module.db.search_movies = lambda query=None, limit=50, offset=0: [
        {"id": "WARM-001", "cover": "https://images.example.test/shared.jpg"},
        {"id": "WARM-002", "cover": ""},
    ][:limit]
    main_module.db.get_actresses = lambda limit=50, offset=0, *args, **kwargs: [
        {"id": "ACT-WARM-001", "avatar": "https://images.example.test/shared.jpg"},
        {"id": "ACT-WARM-002", "avatar": "https://images.example.test/actress.jpg"},
    ][:limit]
    main_module.schedule_image_cache_warmup = lambda items, source: scheduled.append((items, source))
    client = TestClient(main_module.app)
    try:
        response = client.post("/api/cache/warmup", json={"scope": "recent", "limit": 4})
        invalid_scope = client.post("/api/cache/warmup", json={"scope": "remote_urls", "limit": 4})
        too_many = client.post("/api/cache/warmup", json={"scope": "recent", "limit": 81})
    finally:
        main_module.db.search_movies = original_search_movies
        main_module.db.get_actresses = original_get_actresses
        main_module.schedule_image_cache_warmup = original_schedule

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert response.json()["data"] == {
        "scope": "recent",
        "accepted": 2,
        "in_progress": True,
    }
    assert len(scheduled) == 1
    assert scheduled[0][1] == "manual_recent"
    assert len(scheduled[0][0]) == 4
    assert invalid_scope.status_code == 422
    assert too_many.status_code == 422


def test_retryable_failed_queue_blocks_duplicate(main_module):
    task_id = main_module.db.add_to_queue(
        magnet="magnet:?xt=urn:btih:retryable001",
        movie_id="RETRY-001",
        title="Retryable Failed",
        source_url="scenario",
    )
    main_module.db.update_download_status(task_id, "failed", "temporary qb error")

    existing = main_module.db.get_active_queue_entry(
        movie_id="RETRY-001",
        magnet="magnet:?xt=urn:btih:retryable001",
    )
    assert existing["id"] == task_id
    assert existing["status"] == "failed"
    assert existing["retry_count"] == 1

    duplicate_id = main_module.db.add_to_queue(
        magnet="magnet:?xt=urn:btih:retryable001",
        movie_id="RETRY-001",
        title="Retryable Failed Duplicate",
        source_url="scenario",
    )
    assert duplicate_id == task_id

    conn = main_module.db.get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as count FROM download_queue WHERE movie_id = ?", ("RETRY-001",))
        assert cursor.fetchone()["count"] == 1
    finally:
        main_module.db._release_connection(conn)


def test_external_task_queue_is_idempotent_and_survives_service_recreation(main_module):
    from external_task_service import ExternalTaskService

    task_type = "restart_safe_test"
    task = main_module.db.enqueue_external_task(
        task_type,
        "same-work",
        {"value": 7},
        "scenario",
        5,
    )
    duplicate = main_module.db.enqueue_external_task(
        task_type,
        "same-work",
        {"value": 99},
        "scenario",
        5,
    )
    assert duplicate["id"] == task["id"]
    assert duplicate["payload"] == {"value": 7}

    recreated = ExternalTaskService(
        db=main_module.db,
        logger=main_module.logger,
        safe_exception_label=main_module.safe_exception_label,
    )
    recreated.register_handler(task_type, lambda payload: {"processed": payload["value"]})
    summary = recreated.run_due(task_type)
    persisted = main_module.db.get_external_task(task_type, "same-work")

    assert summary["completed"] == 1
    assert persisted["status"] == "completed"
    assert persisted["result"] == {"processed": 7}

    sensitive = main_module.db.enqueue_external_task(
        task_type,
        "sensitive-payload",
        {
            "token": "must-not-persist",
            "callback": "https://example.test/sync?api_key=must-not-persist",
        },
    )
    assert "must-not-persist" not in str(sensitive["payload"])
    assert sensitive["payload"]["token"] == "<redacted>"


def test_external_task_retry_compacts_failures_and_records_recovery(main_module):
    from external_task_service import ExternalTaskService

    task_type = "retry_recovery_test"
    service = ExternalTaskService(
        db=main_module.db,
        logger=main_module.logger,
        safe_exception_label=main_module.safe_exception_label,
    )
    outcomes = [ConnectionError("offline"), TimeoutError("still offline"), {"synced": 3}]

    def handler(_payload):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    service.register_handler(task_type, handler)
    service.enqueue(task_type, "recover", max_retries=5)

    first = service.run_due(task_type)
    failed_once = main_module.db.get_external_task(task_type, "recover")
    assert first["deferred"] == 1
    assert failed_once["retry_count"] == 1
    assert failed_once["last_error"] == "ConnectionError"
    first_error_at = failed_once["first_error_at"]

    def make_due():
        conn = main_module.db.get_connection()
        try:
            conn.execute(
                "UPDATE external_tasks SET next_retry_at = 0 WHERE task_type = ? AND dedupe_key = ?",
                (task_type, "recover"),
            )
            conn.commit()
        finally:
            main_module.db._release_connection(conn)

    make_due()
    second = service.run_due(task_type)
    failed_twice = main_module.db.get_external_task(task_type, "recover")
    assert second["deferred"] == 1
    assert failed_twice["retry_count"] == 2
    assert failed_twice["first_error_at"] == first_error_at
    assert failed_twice["last_error"] == "TimeoutError"

    make_due()
    recovered = service.run_due(task_type)
    completed = main_module.db.get_external_task(task_type, "recover")
    assert recovered["completed"] == 1
    assert recovered["recovered"] == 1
    assert recovered["retry_count"] == 2
    assert completed["status"] == "completed"
    assert completed["retry_count"] == 2
    assert completed["result"] == {"synced": 3}
    assert completed["last_error"] == "TimeoutError"


def test_external_task_batch_isolates_failures_and_recovers_expired_lease(main_module):
    from external_task_service import ExternalTaskService

    task_type = "batch_isolation_test"
    service = ExternalTaskService(
        db=main_module.db,
        logger=main_module.logger,
        safe_exception_label=main_module.safe_exception_label,
    )

    def handler(payload):
        if payload["key"] == "bad":
            raise ConnectionError("offline")
        return {"key": payload["key"]}

    service.register_handler(task_type, handler)
    service.enqueue(task_type, "bad", payload={"key": "bad"})
    service.enqueue(task_type, "good", payload={"key": "good"})
    summary = service.run_due(task_type, limit=10)
    assert summary["claimed"] == 2
    assert summary["completed"] == 1
    assert summary["deferred"] == 1
    assert main_module.db.get_external_task(task_type, "good")["status"] == "completed"
    assert main_module.db.get_external_task(task_type, "bad")["status"] == "waiting_external_service"

    lease_type = "lease_recovery_test"
    main_module.db.enqueue_external_task(lease_type, "leased")
    claimed = main_module.db.claim_due_external_tasks(lease_type, lease_seconds=30, now=1000)
    assert len(claimed) == 1
    assert main_module.db.claim_due_external_tasks(lease_type, now=1010) == []
    reclaimed = main_module.db.claim_due_external_tasks(lease_type, now=1031)
    assert len(reclaimed) == 1
    assert reclaimed[0]["id"] == claimed[0]["id"]


def test_jellyfin_scheduled_sync_uses_persistent_external_queue(main_module):
    task_key = "2099-12-31"
    original_sync = main_module.sync_jellyfin_status_report
    calls = []

    def fake_sync(**kwargs):
        calls.append(kwargs)
        return {"watched": 2, "errors": []}

    main_module.sync_jellyfin_status_report = fake_sync
    try:
        first = main_module.enqueue_jellyfin_automation_sync(task_key)
        duplicate = main_module.enqueue_jellyfin_automation_sync(task_key)
        summary = main_module.process_jellyfin_automation_tasks()
    finally:
        main_module.sync_jellyfin_status_report = original_sync

    persisted = main_module.db.get_external_task(
        "jellyfin_automation_sync",
        f"daily:{task_key}",
    )
    assert first["id"] == duplicate["id"]
    assert summary["completed"] == 1
    assert persisted["status"] == "completed"
    assert persisted["result"] == {"watched": 2, "errors": []}
    assert calls == [{
        "approval_actor": "system",
        "approval_reason": "Scheduled Jellyfin automation sync",
    }]


def test_external_task_stops_at_retry_limit_and_is_visible_in_task_status(main_module):
    from external_task_service import ExternalTaskService
    from fastapi.testclient import TestClient

    task_type = "retry_limit_test"
    service = ExternalTaskService(
        db=main_module.db,
        logger=main_module.logger,
        safe_exception_label=main_module.safe_exception_label,
    )
    service.register_handler(
        task_type,
        lambda _payload: (_ for _ in ()).throw(ConnectionError("offline")),
    )
    service.enqueue(task_type, "limited", max_retries=2)
    assert service.run_due(task_type)["deferred"] == 1

    conn = main_module.db.get_connection()
    try:
        conn.execute(
            "UPDATE external_tasks SET next_retry_at = 0 WHERE task_type = ? AND dedupe_key = ?",
            (task_type, "limited"),
        )
        conn.commit()
    finally:
        main_module.db._release_connection(conn)

    exhausted = service.run_due(task_type)
    persisted = main_module.db.get_external_task(task_type, "limited")
    assert exhausted["needs_attention"] == 1
    assert persisted["status"] == "needs_attention"
    assert persisted["retry_count"] == 2
    assert main_module.db.claim_due_external_tasks(task_type) == []

    response = TestClient(main_module.app).get("/api/tasks/status")
    assert response.status_code == 200
    external_queue = response.json()["data"]["external_queue"]
    assert external_queue["available"] is True
    assert external_queue["needs_attention"] >= 1


def test_external_task_list_is_filterable_and_hides_internal_payload(main_module):
    from fastapi.testclient import TestClient

    task = main_module.db.enqueue_external_task(
        "task_center_test",
        "private-dedupe-key",
        payload={"token": "secret-value", "movie_id": "TASK-CENTER-001"},
        source="worker token=secret-value",
        max_retries=5,
    )
    main_module.db.defer_external_task(task["id"], "service token=secret-value offline")

    client = TestClient(main_module.app)
    response = client.get(
        "/api/tasks/external",
        params={"status": "waiting_external_service", "task_type": "task_center_test"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    row = body["data"][0]
    assert row["id"] == task["id"]
    assert row["retry_count"] == 1
    assert row["status"] == "waiting_external_service"
    assert row["source"] == "worker token=***"
    assert row["last_error"] == "service token=*** offline"
    assert "dedupe_key" not in row
    assert "payload" not in row
    assert "secret-value" not in response.text

    invalid = client.get("/api/tasks/external", params={"status": "stuck_forever"})
    assert invalid.status_code == 422


def test_external_service_retry_records_first_error_and_final_recovery(main_module):
    movie_id = "WAIT-RECOVERY-001"
    magnet = "magnet:?xt=urn:btih:waitrecovery001"
    task_id = main_module.db.add_to_queue(
        magnet=magnet,
        movie_id=movie_id,
        title="Wait and recover",
        source_url="subscription",
    )

    for _ in range(3):
        main_module.db.defer_download_for_external_service(
            task_id,
            "qb_unavailable",
            base_delay_seconds=1,
            max_delay_seconds=1,
        )

    waiting = main_module.db.get_active_queue_entry(movie_id=movie_id, magnet=magnet)
    assert waiting["status"] == "waiting_external_service"
    assert waiting["retry_count"] == 3
    assert waiting["error_message"] == "qb_unavailable"
    assert waiting["next_retry_at"] > time.time()

    main_module.db.update_download_status(task_id, "submitted")
    events = main_module.db.get_movie_lifecycle_events(movie_id, 20)
    waiting_events = [event for event in events if event["event_type"] == "download_waiting_external_service"]
    submitted_events = [event for event in events if event["event_type"] == "download_submitted"]
    assert len(waiting_events) == 1
    assert len(submitted_events) == 1
    assert submitted_events[0]["reason"] == "Recovered after 3 retries"
    assert submitted_events[0]["payload"]["retry_count"] == 3


def test_dispatcher_defers_all_fetched_tasks_when_qb_is_offline(main_module):
    service = main_module.get_download_service()
    task_rows = []
    for index in range(3):
        movie_id = f"WAIT-BATCH-{index}"
        magnet = f"magnet:?xt=urn:btih:waitbatch{index:03d}"
        task_id = main_module.db.add_to_queue(magnet, movie_id, f"Wait batch {index}", "subscription")
        task_rows.append({
            "id": task_id,
            "movie_id": movie_id,
            "magnet": magnet,
            "title": f"Wait batch {index}",
        })

    class StopAfterOneCycle:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _timeout):
            self.stopped = True
            return True

    original_stop_event = service.stop_event
    original_get_qb_session = service.get_qb_session
    original_get_pending = service.db.get_pending_downloads
    service.stop_event = StopAfterOneCycle()
    service.get_qb_session = lambda: (None, None)
    service.db.get_pending_downloads = lambda limit=5: task_rows
    try:
        service.download_dispatcher_task()
    finally:
        service.stop_event = original_stop_event
        service.get_qb_session = original_get_qb_session
        service.db.get_pending_downloads = original_get_pending

    for task in task_rows:
        queued = main_module.db.get_active_queue_entry(
            movie_id=task["movie_id"],
            magnet=task["magnet"],
        )
        assert queued["status"] == "waiting_external_service"
        assert queued["retry_count"] == 1


def test_task_registry_compacts_retries_into_recovery_summary():
    from task_registry import TaskRegistry

    registry = TaskRegistry()
    registry.register("external_sync", description="External sync", category="test")
    registry.mark_error("external_sync", ConnectionError("first"))
    registry.mark_error(
        "external_sync",
        TimeoutError("second"),
        summary={"processed": 2, "failed": 1},
    )

    failed = registry.snapshot()["tasks"][0]
    assert failed["consecutive_error_count"] == 2
    assert failed["total_error_count"] == 2
    assert failed["first_error_at"] is not None
    assert failed["last_error"]["type"] == "TimeoutError"
    assert failed["last_summary"] == {"processed": 2, "failed": 1}

    registry.mark_success("external_sync", summary={"processed": 1})
    recovered = registry.snapshot()["tasks"][0]
    assert recovered["consecutive_error_count"] == 0
    assert recovered["total_error_count"] == 2
    assert recovered["last_error"] is None
    assert recovered["last_recovery"]["retry_count"] == 2
    assert recovered["last_recovery"]["last_error"]["type"] == "TimeoutError"


def test_image_cache_warmup_isolates_single_item_failure(temp_dir):
    from image_cache_service import ImageCacheService

    service = ImageCacheService(str(Path(temp_dir.name) / "warmup-isolation"))
    service.find_cached_original = lambda _url: None

    def cache_url(url):
        if "bad.example" in url:
            raise ConnectionError("image source offline")
        return str(Path(temp_dir.name) / "cached.jpg")

    service.cache_remote_url = cache_url
    summary = service.warm_remote_cache([
        "https://bad.example/cover.jpg",
        "https://good.example/cover.jpg",
    ])

    assert summary == {
        "total": 2,
        "hit": 0,
        "cached": 1,
        "failed": 1,
        "thumbs": 0,  # 预热只喂了 cover，演员头像不在本次统计内
    }


def test_actress_metadata_retry_audit_survives_updates_and_records_recovery(main_module):
    actress_id = "METADATA-RETRY-001"
    assert main_module.db.save_actress({
        "id": actress_id,
        "name": "Metadata Retry",
        "metadata_requested_at": time.time(),
        "metadata_attempt_count": 0,
    }) is True

    main_module.db.mark_actress_metadata_attempt(
        actress_id,
        success=False,
        error="ConnectionError",
    )
    failed = main_module.db.get_actress(actress_id)
    assert failed["metadata_attempt_count"] == 1
    assert failed["metadata_first_error_at"] is not None
    assert failed["metadata_last_error"] == "ConnectionError"
    assert failed["metadata_completed_at"] is None
    first_error_at = failed["metadata_first_error_at"]

    assert main_module.db.save_actress({
        "id": actress_id,
        "name": "Metadata Retry Updated",
        "avatar": "https://example.test/avatar.jpg",
    }) is True
    preserved = main_module.db.get_actress(actress_id)
    assert preserved["metadata_attempt_count"] == 1
    assert preserved["metadata_first_error_at"] == first_error_at
    assert preserved["metadata_last_error"] == "ConnectionError"

    main_module.db.mark_actress_metadata_attempt(actress_id, success=True)
    recovered = main_module.db.get_actress(actress_id)
    assert recovered["metadata_attempt_count"] == 2
    assert recovered["metadata_requested_at"] is None
    assert recovered["metadata_completed_at"] is not None
    assert recovered["metadata_first_error_at"] == first_error_at
    assert recovered["metadata_last_error"] == "ConnectionError"

    stats = main_module.db.get_actress_metadata_task_stats(5)
    assert stats["completed"] >= 1


def test_lifecycle_records_six_dimensions_and_transition_audit(main_module):
    from fastapi.testclient import TestClient

    movie_id = "LIFECYCLE-DIMENSIONS-001"
    assert main_module.db.record_lifecycle_event(
        movie_id,
        "content_discovered",
        title="Lifecycle dimensions",
        discovery_status="discovered",
        source="discovery:jable",
        reason="normalized discovery result",
        execution_result="success",
    ) is True
    assert main_module.db.record_lifecycle_event(
        movie_id,
        "download_candidate_created",
        decision_status="candidate",
        source="subscription",
        reason="subscription matched",
        execution_result="success",
    ) is True
    assert main_module.db.record_lifecycle_event(
        movie_id,
        "download_candidate_approved",
        decision_status="approved",
        source="user",
        reason="manual approval",
        execution_result="success",
    ) is True
    assert main_module.db.record_lifecycle_event(
        movie_id,
        "organize_failed",
        organize_status="failed",
        source="organizer",
        reason="file operation failed",
        execution_result="failed",
        error_summary="path C:/private/movie.mp4 api_key=secret-token",
    ) is True

    lifecycle = main_module.db.get_movie_lifecycle(movie_id=movie_id)[0]
    assert lifecycle["discovery_status"] == "discovered"
    assert lifecycle["decision_status"] == "approved"
    assert lifecycle["download_status"] == "unknown"
    assert lifecycle["organize_status"] == "failed"
    assert lifecycle["jellyfin_status"] == "unknown"
    assert lifecycle["cleanup_status"] == "none"

    events = main_module.db.get_movie_lifecycle_events(movie_id, 20)
    by_type = {event["event_type"]: event for event in events}
    assert by_type["content_discovered"]["status_dimension"] == "discovery_status"
    assert by_type["content_discovered"]["old_status"] == "unknown"
    assert by_type["content_discovered"]["new_status"] == "discovered"
    assert by_type["download_candidate_created"]["old_status"] == "pending"
    assert by_type["download_candidate_created"]["new_status"] == "candidate"
    assert by_type["download_candidate_approved"]["old_status"] == "candidate"
    assert by_type["download_candidate_approved"]["new_status"] == "approved"
    assert by_type["organize_failed"]["old_status"] == "pending"
    assert by_type["organize_failed"]["new_status"] == "failed"
    assert by_type["organize_failed"]["execution_result"] == "failed"
    assert "secret-token" not in str(by_type["organize_failed"])
    assert "C:/private" not in str(by_type["organize_failed"])

    response = TestClient(main_module.app).get(
        "/api/lifecycle/movies",
        params={
            "discovery_status": "discovered",
            "decision_status": "approved",
            "organize_status": "failed",
        },
    )
    assert response.status_code == 200
    assert any(row["movie_id"] == movie_id for row in response.json()["data"])


def test_discovery_records_first_discovery_transition_once(main_module):
    service = main_module.get_discovery_service()
    movie_id = "DISCOVERY-LIFECYCLE-001"
    original_reload = service.discovery_scraper.reload_config
    original_hot = service.jable_scraper.get_hot_movies
    service.discovery_scraper.reload_config = lambda: None
    service.jable_scraper.get_hot_movies = lambda **kwargs: [{
        "id": movie_id,
        "title": "Discovery lifecycle",
        "detail_url": f"https://example.test/{movie_id}",
    }]
    try:
        service.fetch_movies("jable", 1, "daily")
        service.fetch_movies("jable", 1, "daily")
    finally:
        service.discovery_scraper.reload_config = original_reload
        service.jable_scraper.get_hot_movies = original_hot

    lifecycle = main_module.db.get_movie_lifecycle(movie_id=movie_id)[0]
    events = main_module.db.get_movie_lifecycle_events(movie_id, 20)
    discovered_events = [event for event in events if event["event_type"] == "content_discovered"]
    assert lifecycle["discovery_status"] == "discovered"
    assert len(discovered_events) == 1
    assert discovered_events[0]["old_status"] == "unknown"
    assert discovered_events[0]["new_status"] == "discovered"


def test_organizer_enforces_managed_source_and_records_transitions(main_module, temp_dir):
    managed_root = Path(temp_dir.name) / "managed"
    unmanaged_root = Path(temp_dir.name) / "unmanaged"
    library_root = managed_root / "library"
    managed_root.mkdir()
    unmanaged_root.mkdir()
    managed_source = managed_root / "managed.mp4"
    unmanaged_source = unmanaged_root / "unmanaged.mp4"
    managed_source.write_bytes(b"managed movie")
    unmanaged_source.write_bytes(b"unmanaged movie")
    original_managed_roots = os.environ.get("BUS_TERMINAL_MANAGED_MEDIA_ROOTS")
    os.environ["BUS_TERMINAL_MANAGED_MEDIA_ROOTS"] = str(managed_root)

    blocked_id = "ORGANIZE-BLOCKED-001"
    completed_id = "ORGANIZE-COMPLETED-001"
    main_module.db.save_movie({"id": blocked_id, "title": "Blocked", "detail": {}})
    main_module.db.save_movie({"id": completed_id, "title": "Completed", "detail": {}})
    organizer = main_module.organizer.Organizer(allow_file_writes=True)
    organizer.library_root = str(library_root)
    try:
        assert organizer.create_symlink_for_movie(blocked_id, str(unmanaged_source)) is False
        assert organizer.create_symlink_for_movie(completed_id, str(managed_source)) is True
    finally:
        if original_managed_roots is None:
            os.environ.pop("BUS_TERMINAL_MANAGED_MEDIA_ROOTS", None)
        else:
            os.environ["BUS_TERMINAL_MANAGED_MEDIA_ROOTS"] = original_managed_roots

    blocked = main_module.db.get_movie_lifecycle(movie_id=blocked_id)[0]
    completed = main_module.db.get_movie_lifecycle(movie_id=completed_id)[0]
    completed_events = main_module.db.get_movie_lifecycle_events(completed_id, 20)
    event_types = [event["event_type"] for event in reversed(completed_events)]
    assert blocked["organize_status"] == "blocked"
    assert completed["organize_status"] == "completed"
    assert event_types == ["organize_started", "organize_completed"]
    assert completed_events[0]["old_status"] == "organizing"
    assert completed_events[0]["new_status"] == "completed"


def test_subscription_crawl_continues_after_one_source_crashes(main_module):
    calls = []

    class StopAfterOneCycle:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _timeout):
            self.stopped = True
            return True

    original_stop_event = main_module.stop_event
    original_get_subscriptions = main_module.db.get_subscriptions
    original_process_subscription = main_module.process_subscription
    original_paused = main_module.GLOBAL_SUBSCRIPTION_PAUSED
    main_module.stop_event = StopAfterOneCycle()
    main_module.GLOBAL_SUBSCRIPTION_PAUSED = False
    main_module.db.get_subscriptions = lambda: [{"url": "first"}, {"url": "second"}]

    def process(sub, force_refresh=False):
        calls.append((sub["url"], force_refresh))
        if sub["url"] == "first":
            raise ConnectionError("source offline")
        return {"failed_count": 0, "pending_count": 1, "added_count": 0}

    main_module.process_subscription = process
    try:
        main_module.auto_crawl_subscriptions()
    finally:
        main_module.stop_event = original_stop_event
        main_module.db.get_subscriptions = original_get_subscriptions
        main_module.process_subscription = original_process_subscription
        main_module.GLOBAL_SUBSCRIPTION_PAUSED = original_paused

    assert calls == [("first", True), ("second", True)]
    task = next(
        item for item in main_module.task_registry.snapshot()["tasks"]
        if item["name"] == "auto_crawl_subscriptions"
    )
    assert task["consecutive_error_count"] >= 1


def test_managed_file_delete_guard(main_module, temp_dir):
    managed_root = Path(temp_dir.name) / "managed_media"
    unmanaged_root = Path(temp_dir.name) / "unmanaged_media"
    managed_root.mkdir()
    unmanaged_root.mkdir()
    os.environ["BUS_TERMINAL_MANAGED_MEDIA_ROOTS"] = str(managed_root)

    managed_file = managed_root / "managed.mp4"
    unmanaged_file = unmanaged_root / "unmanaged.mp4"
    managed_file.write_text("managed", encoding="utf-8")
    unmanaged_file.write_text("unmanaged", encoding="utf-8")

    conn = main_module.db.get_connection()
    try:
        cursor = conn.cursor()
        insert_sql = (
            "INSERT INTO movies (id, title, scraped_at) VALUES (%s, %s, %s)"
            if main_module.db.is_postgres
            else "INSERT INTO movies (id, title, scraped_at) VALUES (?, ?, ?)"
        )
        local_sql = (
            "INSERT INTO local_files (movie_id, file_path, added_at) VALUES (%s, %s, %s)"
            if main_module.db.is_postgres
            else "INSERT INTO local_files (movie_id, file_path, added_at) VALUES (?, ?, ?)"
        )
        cursor.execute(insert_sql, ("SAFE-001", "Managed", 1))
        cursor.execute(local_sql, ("SAFE-001", str(managed_file), 1))
        cursor.execute(insert_sql, ("SAFE-002", "Unmanaged", 1))
        cursor.execute(local_sql, ("SAFE-002", str(unmanaged_file), 1))
        conn.commit()
    finally:
        main_module.db._release_connection(conn)

    assert main_module.db.delete_movie_full("SAFE-002", delete_file=True) is True
    assert unmanaged_file.exists()
    assert main_module.db.delete_movie_full("SAFE-001", delete_file=True) is True
    assert not managed_file.exists()

    trash_file = managed_root / "广告.txt"
    keep_file = managed_root / "movie.mp4"
    small_video = managed_root / "ad_fragment.mp4"
    outside_trash = unmanaged_root / "广告.txt"
    trash_file.write_text("trash", encoding="utf-8")
    # 400MB 以下的视频按新策略算碎片（见 media_policy），所以「该保留的媒体文件」
    # 必须是真·成品体积：用稀疏文件占位，不实际写 400MB 数据。
    with keep_file.open("wb") as fh:
        fh.truncate(media_policy.MIN_PLAYABLE_VIDEO_BYTES + 1024)
    small_video.write_text("tiny", encoding="utf-8")
    outside_trash.write_text("trash", encoding="utf-8")

    main_module.lifecycle_manager.lifecycle_manager.clean_ads_post_download(str(unmanaged_root))
    assert outside_trash.exists()
    main_module.lifecycle_manager.lifecycle_manager.clean_ads_post_download(str(managed_root))
    assert not trash_file.exists()
    assert not small_video.exists()   # 400MB 以下视频 = 碎片，直接清
    assert keep_file.exists()          # 成品体积的视频照旧保留


def test_cors_wildcard_disables_credentials():
    import importlib
    import config

    original_value = os.environ.get("CORS_ORIGINS")
    try:
        os.environ.pop("CORS_ORIGINS", None)
        importlib.reload(config)
        assert config.get_cors_origins()
        assert config.get_cors_allow_credentials() is True

        os.environ["CORS_ORIGINS"] = "*"
        importlib.reload(config)
        assert config.get_cors_origins() == ["*"]
        assert config.get_cors_allow_credentials() is False

        os.environ["CORS_ORIGINS"] = (
            "http://localhost:3000,http://example.test,"
            "file://bad.test,https://bad.test/path,http://user:pass@bad.test"
        )
        importlib.reload(config)
        assert config.get_cors_origins() == ["http://localhost:3000", "http://example.test"]
        assert config.get_cors_allow_credentials() is True
    finally:
        if original_value is None:
            os.environ.pop("CORS_ORIGINS", None)
        else:
            os.environ["CORS_ORIGINS"] = original_value
        importlib.reload(config)


def test_extra_domains_only_accepts_http_urls():
    import importlib
    import config

    original_value = os.environ.get("EXTRA_DOMAINS")
    try:
        os.environ["EXTRA_DOMAINS"] = (
            "https://www.seedmm.cyou; http://example.test/ ; "
            "httpx://bad.test ftp://bad.test https://www.seedmm.cyou "
            "https:///missing-host"
        )
        importlib.reload(config)
        assert config.get_extra_domains() == [
            "https://www.seedmm.cyou",
            "http://example.test",
        ]
    finally:
        if original_value is None:
            os.environ.pop("EXTRA_DOMAINS", None)
        else:
            os.environ["EXTRA_DOMAINS"] = original_value
        importlib.reload(config)


def test_numeric_environment_config_falls_back_on_dirty_values():
    import importlib
    import config

    keys = {
        "SUBSCRIPTION_CHECK_INTERVAL": 4 * 3600,
        "GROOM_TASK_INTERVAL": 60,
        "SEEDING_MAINTENANCE_INTERVAL": 4 * 3600,
        "DISK_MONITOR_INTERVAL": 60,
        "DISK_SPACE_THRESHOLD": 800 * 1024 * 1024 * 1024,
        "SEEDING_TIME_LIMIT": 60,
        "DOWNLOAD_DISPATCHER_INTERVAL": 5,
        "HOMEPAGE_REFRESH_INTERVAL": 600,
        "JABLE_HOT_CHECK_INTERVAL": 12 * 3600,
        "TRANSLATION_BATCH_SIZE": 80,
    }
    original_values = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            os.environ[key] = "not-a-number"
        os.environ["GROOM_TASK_INTERVAL"] = "-10"
        os.environ["SEEDING_TIME_LIMIT"] = "-1"

        importlib.reload(config)

        assert config.SUBSCRIPTION_CHECK_INTERVAL == keys["SUBSCRIPTION_CHECK_INTERVAL"]
        assert config.GROOM_TASK_INTERVAL == keys["GROOM_TASK_INTERVAL"]
        assert config.SEEDING_MAINTENANCE_INTERVAL == keys["SEEDING_MAINTENANCE_INTERVAL"]
        assert config.DISK_MONITOR_INTERVAL == keys["DISK_MONITOR_INTERVAL"]
        assert config.DISK_SPACE_THRESHOLD == keys["DISK_SPACE_THRESHOLD"]
        assert config.SEEDING_TIME_LIMIT == keys["SEEDING_TIME_LIMIT"]
        assert config.DOWNLOAD_DISPATCHER_INTERVAL == keys["DOWNLOAD_DISPATCHER_INTERVAL"]
        assert config.HOMEPAGE_REFRESH_INTERVAL == keys["HOMEPAGE_REFRESH_INTERVAL"]
        assert config.JABLE_HOT_CHECK_INTERVAL == keys["JABLE_HOT_CHECK_INTERVAL"]
        assert config.TRANSLATION_BATCH_SIZE == keys["TRANSLATION_BATCH_SIZE"]
    finally:
        for key, value in original_values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(config)


def test_main_import_tolerates_dirty_runtime_environment_values():
    with tempfile.TemporaryDirectory(prefix="busterminal_dirty_env_") as data_dir:
        env = os.environ.copy()
        env.update({
            "DATA_DIR": data_dir,
            "DATABASE_URL": "",
            "BUS_TERMINAL_DISABLE_STARTUP_TASKS": "true",
            "BUS_TERMINAL_DISABLE_DOMAIN_ROTATION": "true",
            "BUS_TERMINAL_SUBSCRIPTION_SYNC_TIMEOUT": "not-a-float",
            "BUS_TERMINAL_DISCOVERY_CACHE_TTL": "not-an-int",
            "BUS_TERMINAL_INFORMATION_FLOW_PRELOAD_PAGES": "-9",
            "LOG_LEVEL": "ERROR",
        })
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import os; import main; "
                    "expected_path = os.path.join(os.environ['DATA_DIR'], 'movies.db').replace('\\\\', '/'); "
                    "assert main.SUBSCRIPTION_JELLYFIN_SYNC_TIMEOUT == 3.0; "
                    "assert main.DISCOVERY_CACHE_TTL == 21600; "
                    "assert main.INFORMATION_FLOW_PRELOAD_PAGES == 3; "
                    "assert main.config.DATABASE_URL == f'sqlite:///{expected_path}'; "
                    "assert main.db.db_path.replace('\\\\', '/') == expected_path"
                ),
            ],
            cwd=Path(__file__).resolve().parent,
            env=env,
            capture_output=True,
            text=True,
            # 子进程 import main 会做域名探测等真实网络 I/O，30s 在 CI/并行跑测时不够用。
            timeout=120,
            check=False,
        )
        assert result.returncode == 0, result.stderr or result.stdout


def main():
    temp_dir = _prepare_temp_environment()
    try:
        import database

        database.db.set_config("enable_domain_rotation", "false")
        database.db.set_config("fixed_domain", "https://www.javbus.com")

        import main as app_main

        _patch_external_services(app_main)
        test_jellyfin_delete_guard(app_main)
        test_jellyfin_delete_rejects_mixed_invalid_item_ids(app_main)
        test_jellyfin_delete_guard_redacts_reason_in_logs(app_main)
        test_scrape_monitor_url_logs_are_private_by_default(app_main)
        test_runtime_bool_config_accepts_common_truthy_values(app_main)
        test_jellyfin_item_id_normalization_rejects_path_injection(app_main)
        test_jellyfin_client_tolerates_dirty_library_payload(app_main)
        test_jellyfin_client_returns_only_dict_items_from_collection_endpoints(app_main)
        test_delete_task_id_normalization_rejects_invalid_values(app_main)
        asyncio.run(test_delete_task_endpoints_reject_invalid_task_id(app_main))
        test_jellyfin_client_rejects_invalid_item_ids_before_remote_request(app_main)
        test_jellyfin_bulk_delete_errors_are_sanitized(app_main)
        asyncio.run(test_jellyfin_image_rejects_invalid_item_id_before_remote_request(app_main))
        test_legacy_jellyfin_cleanup_delete_guard(app_main)
        test_legacy_jellyfin_cleanup_rejects_invalid_item_id(app_main)
        test_legacy_jellyfin_cleanup_tolerates_dirty_config(app_main)
        test_empty_directory_cleanup_respects_managed_roots(app_main, temp_dir)
        test_cleanup_candidate_is_non_destructive(app_main)
        test_download_permission_guardrails(app_main)
        asyncio.run(test_jellyfin_webhook_creates_candidates_only(app_main))
        test_jellyfin_webhook_optional_token_guard(app_main)
        asyncio.run(test_jellyfin_webhook_payload_variants(app_main))
        asyncio.run(test_jellyfin_person_favorite_creates_subscription(app_main))
        asyncio.run(test_jellyfin_favorite_people_sync_creates_subscription(app_main))
        test_actress_subscribe_alias_creates_subscription(app_main)
        asyncio.run(test_actress_subscribe_treats_string_false_as_unsubscribed(app_main))
        test_save_actress_parses_string_follow_state(app_main)
        test_actresses_endpoint_tolerates_dirty_timestamps(app_main)
        asyncio.run(test_jellyfin_active_sync_creates_candidates_only(app_main))
        asyncio.run(test_jellyfin_people_sync_auto_subscribes_known_actress(app_main))
        asyncio.run(test_jellyfin_favorite_actor_creates_subscription(app_main))
        asyncio.run(test_jellyfin_favorite_actor_string_false_unsubscribes(app_main))
        asyncio.run(test_jellyfin_favorite_actor_string_false_removes_legacy_alias_subscriptions(app_main))
        test_jellyfin_favorite_state_parses_string_booleans(app_main)
        asyncio.run(test_jellyfin_favorite_actor_ignores_unsafe_external_subscription_url(app_main))
        asyncio.run(test_jellyfin_favorite_actor_is_visible_in_subscriptions_endpoint(app_main))
        test_jellyfin_favorite_actor_route_is_visible_in_subscriptions_page(app_main)
        asyncio.run(test_jellyfin_person_source_url_is_visible_in_subscriptions_endpoint(app_main))
        asyncio.run(test_jellyfin_favorite_actor_source_url_without_id_is_visible(app_main))
        test_subscription_visibility_accepts_legacy_actor_urls(app_main)
        test_subscription_visibility_accepts_jellyfin_actor_urls(app_main)
        test_subscription_visibility_accepts_jellyfin_person_legacy_equivalents(app_main)
        test_subscription_url_candidates_include_jellyfin_equivalents(app_main)
        test_internal_subscription_url_guardrails(app_main)
        test_subscription_refresh_accepts_internal_alias_without_scraping(app_main)
        asyncio.run(test_subscriptions_endpoint_repairs_raw_jellyfin_person_id(app_main))
        asyncio.run(test_subscriptions_endpoint_returns_when_jellyfin_sync_times_out(app_main))
        asyncio.run(test_subscriptions_endpoint_tolerates_dirty_sync_counts(app_main))
        asyncio.run(test_jellyfin_favorite_actor_endpoint_uses_subscription_helper(app_main))
        asyncio.run(test_subscriptions_endpoint_repairs_jellyfin_actor_subscription(app_main))
        asyncio.run(test_subscriptions_endpoint_repairs_jellyfin_actor_fallback_subscription(app_main))
        asyncio.run(test_subscriptions_endpoint_normalizes_legacy_subscription_names(app_main))
        asyncio.run(test_subscriptions_endpoint_syncs_jellyfin_favorite_people(app_main))
        asyncio.run(test_jellyfin_person_candidate_approval_is_visible_in_subscriptions(app_main))
        asyncio.run(test_automation_candidate_approval_ignores_unsafe_external_subscription_url(app_main))
        asyncio.run(test_automation_candidate_approval_ignores_non_scraper_external_subscription_url(app_main))
        asyncio.run(test_subscriptions_endpoint_syncs_jellyfin_favorite_movie_people(app_main))
        asyncio.run(test_subscriptions_endpoint_syncs_known_jellyfin_person_without_page_url(app_main))
        asyncio.run(test_subscriptions_endpoint_syncs_unknown_jellyfin_movie_people(app_main))
        asyncio.run(test_subscriptions_endpoint_syncs_favorite_movie_people_from_detail(app_main))
        asyncio.run(test_jellyfin_image_fallback_uses_cached_movie_cover(app_main, temp_dir))
        asyncio.run(test_jellyfin_image_proxy_bypasses_invalid_cache_entry(app_main, temp_dir))
        asyncio.run(test_jellyfin_image_proxy_rejects_oversized_stream(app_main, temp_dir))
        asyncio.run(test_jellyfin_image_proxy_rejects_svg_content(app_main, temp_dir))
        test_remote_image_cache_rejects_svg_and_caches_safe_image(app_main, temp_dir)
        test_remote_image_cache_ignores_invalid_cache_entry(app_main, temp_dir)
        asyncio.run(test_proxy_remote_image_rejects_svg(app_main, temp_dir))
        asyncio.run(test_proxy_remote_image_rejects_private_targets_before_fetch(app_main, temp_dir))
        asyncio.run(test_proxy_remote_image_ignores_invalid_cache_entry(app_main, temp_dir))
        test_wiki_infobox_fetch_uses_scraper_proxy(app_main)
        test_jellyfin_routes_are_registered_before_spa_fallback(app_main)
        test_jellyfin_endpoint_errors_are_sanitized(app_main)
        test_spa_fallback_does_not_serve_files_outside_static(app_main)
        test_task_status_errors_are_sanitized(app_main)
        asyncio.run(test_startup_disable_skips_side_effects(app_main))
        asyncio.run(test_startup_does_not_register_empty_off_work_task(app_main))
        asyncio.run(test_automation_candidate_approval_and_rejection(app_main))
        asyncio.run(test_automation_candidate_bulk_approval(app_main))
        asyncio.run(test_user_story_end_to_end_dry_run(app_main))
        test_api_state_endpoints(app_main)
        test_search_endpoint_limit_offset_guardrails(app_main)
        test_discovery_query_guardrails(app_main)
        test_discovery_cache_tolerates_dirty_updated_at(app_main)
        test_dashboard_status_does_not_expose_connection_urls(app_main)
        test_config_secrets_are_masked_and_preserved(app_main)
        test_system_config_default_jellyfin_library_name_is_readable(app_main)
        test_jellyfin_stats_does_not_expose_connection_details(app_main)
        test_jellyfin_stats_tolerates_dirty_numeric_fields(app_main)
        test_jellyfin_library_query_params_are_validated(app_main)
        test_jellyfin_behavior_bucket_error_is_sanitized(app_main)
        test_jellyfin_behavior_helpers_parse_dirty_boolean_and_counts(app_main)
        test_subscription_stats_aggregates_counts_and_caps_recent_tasks(app_main)
        test_subscription_stats_aliases_internal_jellyfin_sources(app_main)
        test_batch_translate_guardrails(app_main)
        test_user_password_helpers_use_strong_hash(app_main)
        test_api_logs_redact_sensitive_values(app_main)
        test_organizer_path_logs_are_redacted_by_default(app_main)
        test_organizer_rejects_library_path_escape(app_main, temp_dir)
        test_organizer_sanitizes_movie_target_paths(app_main, temp_dir)
        test_organizer_actor_thumb_download_blocks_local_urls(app_main, temp_dir)
        test_manual_magnet_download_guardrails(app_main)
        test_movie_download_rejects_unsupported_queue_options(app_main)
        test_scraper_url_guardrails(app_main)
        test_jable_magnet_search_rejects_invalid_video_id(app_main)
        test_genres_endpoint_rejects_invalid_type(app_main)
        test_qb_torrent_control_guardrails(app_main)
        test_qb_torrent_control_request_hashes_are_isolated(app_main)
        test_qb_torrent_list_response_does_not_expose_paths(app_main)
        test_lifecycle_event_payload_does_not_store_paths(app_main)
        test_automation_payload_does_not_store_paths(app_main)
        test_database_bad_json_fields_are_contained(app_main)
        test_qb_session_accepts_host_with_embedded_port(app_main)
        test_qb_api_normalizes_dirty_json_payloads(app_main)
        test_qb_api_normalizes_dirty_list_arguments(app_main)
        test_qb_api_rejects_empty_control_targets_after_normalization(app_main)
        test_qb_dashboard_tolerates_dirty_numeric_fields(app_main)
        test_groom_torrents_fetches_torrents_once(app_main)
        test_groom_torrents_tolerates_dirty_qb_torrent_fields(app_main)
        test_runtime_numeric_config_tolerates_dirty_values(app_main)
        test_jav_file_filter_tags_small_file_only_torrent(app_main)
        test_jav_file_filter_disables_large_non_video_when_no_valid_video(app_main)
        test_jav_file_filter_tolerates_dirty_qb_file_entries(app_main)
        test_lifecycle_pre_download_ad_cleaner_tolerates_dirty_qb_file_entries(app_main)
        test_lifecycle_gc_creates_candidate_without_deleting(app_main, temp_dir)
        test_cache_size_limit_falls_back_for_dirty_config(app_main)
        test_cache_cleanup_only_deletes_safe_image_cache_files(app_main, temp_dir)
        test_retryable_failed_queue_blocks_duplicate(app_main)
        test_managed_file_delete_guard(app_main, temp_dir)
        test_cors_wildcard_disables_credentials()
        test_extra_domains_only_accepts_http_urls()
        test_numeric_environment_config_falls_back_on_dirty_values()
        test_main_import_tolerates_dirty_runtime_environment_values()
        print("lifecycle scenario smoke tests passed")
    finally:
        temp_dir.cleanup()


if __name__ == "__main__":
    main()
