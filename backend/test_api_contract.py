import asyncio
from datetime import datetime
from pathlib import Path
import re

from api_contract import (
    RISK_EXTERNAL_WRITE,
    RISK_LOCAL_WRITE,
    RISK_READ_ONLY,
    classify_operation,
    operation_counts,
)
from api_smoke_cases import MODULE_CASES
from generate_api_docs import render_api_markdown
from movie_normalization import normalize_movie_list


def test_openapi_operations_are_unique_and_risk_classified(main_module):
    schema = main_module.app.openapi()
    operation_ids = []
    operation_total = 0
    for path, path_item in schema["paths"].items():
        for method, operation in path_item.items():
            if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                continue
            operation_total += 1
            operation_ids.append(operation["operationId"])
            assert classify_operation(method, path) in {
                RISK_READ_ONLY,
                RISK_LOCAL_WRITE,
                RISK_EXTERNAL_WRITE,
            }

    assert operation_total >= 75
    assert len(operation_ids) == len(set(operation_ids))
    assert sum(operation_counts(schema).values()) == operation_total


def test_smoke_cases_match_read_only_openapi_contract(main_module):
    schema = main_module.app.openapi()
    case_names = []
    for module_name, cases in MODULE_CASES.items():
        assert cases, f"empty smoke module: {module_name}"
        for case in cases:
            case_names.append(case.name)
            operation = schema["paths"].get(case.path, {}).get("get")
            assert operation, f"missing GET {case.path}"
            assert classify_operation("GET", case.path) == RISK_READ_ONLY
            query_params = {
                item["name"]
                for item in operation.get("parameters", [])
                if item.get("in") == "query"
            }
            assert set(case.params).issubset(query_params)

    assert len(case_names) == len(set(case_names))


def test_sqlite_url_paths_are_cross_platform_safe(main_module):
    from database import sqlite_path_from_url

    assert sqlite_path_from_url("sqlite:////app/data/movies.db") == "/app/data/movies.db"
    assert sqlite_path_from_url("sqlite:///C:/data/movies.db") == "C:/data/movies.db"
    assert sqlite_path_from_url("sqlite:///movies.db") == "movies.db"


def test_every_read_only_operation_resolves_without_internal_error(main_module, monkeypatch):
    from fastapi.testclient import TestClient

    original_jellyfin_host = main_module.db.get_config("jellyfin_host")
    original_jellyfin_api_key = main_module.db.get_config("jellyfin_api_key")
    main_module.db.set_config("jellyfin_host", "")
    main_module.db.set_config("jellyfin_api_key", "")
    main_module.jellyfin_client.reload_config()
    discovery = main_module.get_discovery_service()
    monkeypatch.setattr(main_module, "start_background_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(main_module.scraper, "get_navigation", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(main_module.scraper, "get_genres", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(main_module.scraper, "get_movies", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(main_module.scraper, "get_movie_detail", lambda *_args, **_kwargs: {
        "title": "Probe detail",
        "info": {"id": "PROBE-001"},
        "magnets": [],
    })
    monkeypatch.setattr(main_module.scraper, "refresh_homepage", lambda: 0)
    monkeypatch.setattr(main_module.scraper, "refresh_category", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(main_module.jable_scraper, "get_hot_movies", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(discovery, "get_cached_movies", lambda source, page, rank_type, force_refresh=False: {
        "data": [{"id": "PROBE-001", "title": "Probe", "detail_url": "https://www.javbus.com/PROBE-001"}],
        "source": source,
        "page": page,
        "cached": True,
    })
    monkeypatch.setattr(discovery, "get_raw_movies", lambda source, page, rank_type: {
        "data": [{"id": "PROBE-001", "title": "Probe"}],
        "source": source,
        "page": page,
    })
    monkeypatch.setattr(main_module, "run_jav_file_filter_once", lambda: {"success": True, "processed": 0})
    monkeypatch.setattr(main_module, "get_qb_session", lambda: (None, None))
    monkeypatch.setattr(main_module, "sync_jellyfin_status_report", lambda **kwargs: {"skipped": True})
    monkeypatch.setattr(main_module.get_automation_service(), "approve_all", lambda *args, **kwargs: {"approved": 0})
    monkeypatch.setattr(main_module.get_qb_service(), "session_provider", lambda: (None, None))

    try:
        client = TestClient(main_module.app, raise_server_exceptions=False)
        schema = main_module.app.openapi()
        failures = []
        for path, path_item in schema["paths"].items():
            if not path.startswith("/api/"):
                continue
            request_path = re.sub(r"\{[^}]+\}", "contract-probe", path)
            for method, operation in path_item.items():
                if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                    continue
                if classify_operation(method, path) != RISK_READ_ONLY:
                    continue
                kwargs = {}
                if operation.get("requestBody"):
                    kwargs["json"] = {}
                response = client.request(method.upper(), request_path, **kwargs)
                route_not_found = response.status_code == 404 and response.json().get("detail") == "Not Found"
                if route_not_found or response.status_code == 405 or response.status_code >= 500 and response.status_code not in {502, 503, 504}:
                    failures.append((method.upper(), path, response.status_code, response.text[:160]))

        assert failures == []
    finally:
        main_module.db.set_config("jellyfin_host", original_jellyfin_host)
        main_module.db.set_config("jellyfin_api_key", original_jellyfin_api_key)
        main_module.jellyfin_client.reload_config()
        main_module._automation_service = None
        main_module._download_service = None
        main_module._qb_service = None


def test_generated_api_document_matches_openapi(main_module):
    expected = render_api_markdown(main_module.app.openapi())
    document = Path(__file__).resolve().parents[1] / "docs" / "API.md"
    assert document.read_text(encoding="utf-8") == expected


def test_movie_normalization_rejects_upstream_error_documents():
    movies = normalize_movie_list([
        {"id": "SAFE-001", "title": "Normal title", "detail_url": "https://example.test/SAFE-001"},
        {"id": "BAD-500", "title": "Error 500 (Server Error)!!1", "detail_url": "https://example.test/BAD-500"},
        {"id": "BAD-502", "original_title": "502 Bad Gateway", "detail_url": "https://example.test/BAD-502"},
    ])
    assert [movie["id"] for movie in movies] == ["SAFE-001"]


def test_movie_detail_uses_fresh_structured_cache(main_module):
    # 详情缓存落在 movies.detail（结构化），网络取不到时才回退，不再走通用 cache 表
    url = "https://www.javbus.com/CACHE-DETAIL-001"
    detail = {"title": "Cached detail", "magnets": [], "info": {"id": "CACHE-DETAIL-001"}}
    main_module.db.save_movie({"id": "CACHE-DETAIL-001", "detail": detail})
    original_get = main_module.scraper.session.get
    main_module.scraper.session.get = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network called"))
    try:
        assert main_module.scraper.get_movie_detail(url) == detail
    finally:
        main_module.scraper.session.get = original_get


def test_detail_endpoint_does_not_block_on_translation(main_module):
    calls = []
    original_get_detail = main_module.scraper.get_movie_detail
    original_queue_metadata = main_module.queue_actress_metadata_from_detail
    main_module.scraper.get_movie_detail = lambda url, perform_translate=True: (
        calls.append((url, perform_translate))
        or {"title": "Raw detail", "info": {"id": "FAST-001"}, "magnets": []}
    )
    main_module.queue_actress_metadata_from_detail = lambda *_args, **_kwargs: 0
    try:
        response = asyncio.run(main_module.get_movie_detail(
            "https://www.javbus.com/FAST-001",
            movie_id="FAST-001",
            title="Fast detail",
        ))
    finally:
        main_module.scraper.get_movie_detail = original_get_detail
        main_module.queue_actress_metadata_from_detail = original_queue_metadata

    assert response["success"] is True
    assert calls == [("https://www.javbus.com/FAST-001", False)]


def test_preload_policy_and_daily_catchup_are_bounded(main_module):
    from discovery_service import MAX_PRELOAD_PAGES

    assert main_module.INFORMATION_FLOW_PRELOAD_PAGES == 3
    assert MAX_PRELOAD_PAGES == 30
    service = main_module.get_maintenance_service()
    assert service.should_run_daily_maintenance(datetime(2026, 7, 20, 1, 59), "2026-07-19") is False
    assert service.should_run_daily_maintenance(datetime(2026, 7, 20, 6, 30), "2026-07-19") is True
    assert service.should_run_daily_maintenance(datetime(2026, 7, 20, 23, 0), "2026-07-20") is False


def test_task_registry_exposes_safe_error_codes_without_leaking_secrets():
    from task_registry import TaskRegistry

    registry = TaskRegistry()
    registry.register("safe-code", description="Safe code", category="test")
    registry.mark_error("safe-code", RuntimeError("jellyfin_not_configured"))
    assert registry.snapshot()["tasks"][0]["last_error"]["message"] == "jellyfin_not_configured"

    registry.register("secret", description="Secret", category="test")
    registry.mark_error("secret", RuntimeError("password=do-not-leak"))
    secret_task = next(task for task in registry.snapshot()["tasks"] if task["name"] == "secret")
    assert secret_task["last_error"]["message"] == "RuntimeError"


def test_partial_image_warmup_is_degraded_success(main_module):
    class StopAfterCycle:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _seconds):
            self.stopped = True

    original_stop_event = main_module.stop_event
    original_search_movies = main_module.db.search_movies
    original_get_actresses = main_module.db.get_actresses
    original_warm = main_module.image_cache_adapter.warm_remote_cache
    main_module.stop_event = StopAfterCycle()
    main_module.db.search_movies = lambda *args, **kwargs: [{"cover": "https://images.test/a.jpg"}]
    main_module.db.get_actresses = lambda *args, **kwargs: []
    main_module.image_cache_adapter.warm_remote_cache = lambda *args, **kwargs: {
        "total": 2,
        "hit": 1,
        "cached": 0,
        "failed": 1,
    }
    main_module.task_registry.register(
        "active_image_downloader_task",
        description="Active image test",
        category="test",
    )
    try:
        main_module.active_image_downloader_task()
    finally:
        main_module.stop_event = original_stop_event
        main_module.db.search_movies = original_search_movies
        main_module.db.get_actresses = original_get_actresses
        main_module.image_cache_adapter.warm_remote_cache = original_warm

    task = next(
        item for item in main_module.task_registry.snapshot()["tasks"]
        if item["name"] == "active_image_downloader_task"
    )
    assert task["consecutive_error_count"] == 0
    assert task["last_summary"]["degraded"] is True


def test_unconfigured_jellyfin_scheduler_is_a_skip_not_an_error(main_module):
    class StopAfterCycle:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _seconds):
            self.stopped = True

    original_stop_event = main_module.stop_event
    original_reload = main_module.jellyfin_client.reload_config
    original_host = main_module.jellyfin_client.host
    original_api_key = main_module.jellyfin_client.api_key
    main_module.stop_event = StopAfterCycle()
    main_module.jellyfin_client.reload_config = lambda: None
    main_module.jellyfin_client.host = ""
    main_module.jellyfin_client.api_key = ""
    main_module.task_registry.register(
        "jellyfin_automation_sync_task",
        description="Jellyfin scheduler test",
        category="test",
    )
    try:
        main_module.jellyfin_automation_sync_task()
    finally:
        main_module.stop_event = original_stop_event
        main_module.jellyfin_client.reload_config = original_reload
        main_module.jellyfin_client.host = original_host
        main_module.jellyfin_client.api_key = original_api_key

    task = next(
        item for item in main_module.task_registry.snapshot()["tasks"]
        if item["name"] == "jellyfin_automation_sync_task"
    )
    assert task["consecutive_error_count"] == 0
    assert task["last_summary"] == {"skipped": True, "reason": "jellyfin_not_configured"}


def test_qb_status_distinguishes_saved_config_from_login_failure(main_module):
    service = main_module.get_qb_service()
    original_config = main_module.db.get_config("qb_config")
    original_login = main_module.qb_api.login
    original_session_provider = service.session_provider
    main_module.db.set_config("qb_config", {
        "host": "https://qb.example.test",
        "port": 443,
        "username": "admin",
        "password": "configured-password",
    })
    main_module.qb_api.login = lambda *_args, **_kwargs: None
    service.session_provider = None
    try:
        session, base_url = service.get_session()
        response = service.get_status()
    finally:
        main_module.qb_api.login = original_login
        service.session_provider = original_session_provider
        main_module.db.set_config("qb_config", original_config)

    assert session is None
    assert base_url == "https://qb.example.test:443"
    # 已保存配置 ≠ 未配置：登录失败也要报 configured=True，并且保留 qb_login_failed 这个可诊断原因
    assert response == {
        "success": False,
        "connected": False,
        "degraded": True,
        "message": "qb_login_failed",
        "configured": True,
    }


def test_postgres_connection_is_reset_before_pool_reuse():
    from database import Database

    class Connection:
        rollback_calls = 0

        def rollback(self):
            self.rollback_calls += 1

    class Pool:
        returned = []

        def putconn(self, connection):
            self.returned.append(connection)

    database = Database.__new__(Database)
    database.is_postgres = True
    database.pg_pool = Pool()
    connection = Connection()

    database._release_connection(connection)

    assert connection.rollback_calls == 1
    assert database.pg_pool.returned == [connection]


def test_qb_version_is_normalized_once_at_api_boundary():
    import qb_api

    class Response:
        ok = True
        status_code = 200
        text = "v4.6.6\n"

    class Session:
        def get(self, *_args, **_kwargs):
            return Response()

    assert qb_api.get_app_version(Session(), "https://qb.example.test") == "4.6.6"


def test_movie_summary_batch_preserves_existing_detail(main_module):
    database = main_module.db
    database.save_movie({
        "id": "PERF-001",
        "title": "旧标题",
        "original_title": "原始标题",
        "cover": "https://images.example.test/old.jpg",
        "detail_url": "https://www.javbus.com/PERF-001",
        "date": "2026-01-01",
        "detail": {"magnets": [{"name": "existing"}]},
    })

    saved = database.save_movie_summaries_batch([{
        "id": "PERF-001",
        "title": "新标题",
        "original_title": "原始标题",
        "cover": "https://images.example.test/new.jpg",
        "detail_url": "https://www.javbus.com/PERF-001",
        "date": "2026-02-01",
    }])

    movie = database.get_movie("PERF-001")
    assert saved == 1
    assert movie["title"] == "新标题"
    assert movie["detail"] == {"magnets": [{"name": "existing"}]}
