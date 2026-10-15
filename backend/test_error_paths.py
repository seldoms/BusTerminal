import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client(main_module):
    return TestClient(main_module.app, raise_server_exceptions=False)


def test_unhandled_exception_returns_json_500(main_module, client):
    from fastapi.routing import APIRoute

    async def _boom():
        raise RuntimeError("boom")

    # Insert before the SPA catch-all route at the end of main.py.
    main_module.app.router.routes.insert(
        0, APIRoute("/api/__test_unhandled_boom", endpoint=_boom, methods=["GET"])
    )

    response = client.get("/api/__test_unhandled_boom")
    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "success": False,
        "message": "服务器内部错误",
        "error_type": "RuntimeError",
    }


def test_http_exception_keeps_default_json(client):
    response = client.get("/api/__definitely_not_a_route__")
    assert response.status_code == 404
    assert response.json() == {"detail": "API endpoint not found"}


def test_request_validation_error_keeps_default_422(client):
    response = client.get("/api/discovery/movies", params={"page": 0})
    assert response.status_code == 422
    assert "detail" in response.json()


class _EmptyDiscoveryService:
    refresh_scheduler = None

    def normalize_source(self, raw_source):
        return str(raw_source or "").strip().lower() or None

    def normalize_rank_type(self, raw_rank_type):
        return str(raw_rank_type or "").strip().lower() or None

    def get_raw_movies(self, source, page, rank_type):
        return {"data": [], "source": source, "page": page}

    def get_cached_movies(self, source, page, rank_type, force_refresh=False):
        return {
            "data": [],
            "source": source,
            "page": page,
            "empty": True,
            "cached": False,
            "stale": False,
            "cache_status": "miss",
        }


class _FailingDiscoveryService(_EmptyDiscoveryService):
    def get_raw_movies(self, source, page, rank_type):
        raise ConnectionError("source down")

    def get_cached_movies(self, source, page, rank_type, force_refresh=False):
        raise ConnectionError("source down")


def test_discovery_raw_empty_result_returns_200(main_module, client, monkeypatch):
    monkeypatch.setattr(main_module, "get_discovery_service", lambda: _EmptyDiscoveryService())
    response = client.get("/api/discovery/movies/raw")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"] == []


def test_discovery_cached_empty_result_returns_200(main_module, client, monkeypatch):
    monkeypatch.setattr(main_module, "get_discovery_service", lambda: _EmptyDiscoveryService())
    response = client.get("/api/discovery/movies")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"] == []


def test_discovery_source_failure_still_502(main_module, client, monkeypatch):
    monkeypatch.setattr(main_module, "get_discovery_service", lambda: _FailingDiscoveryService())
    for path in ("/api/discovery/movies", "/api/discovery/movies/raw"):
        response = client.get(path)
        assert response.status_code == 502
        assert response.json() == {"detail": "discovery_fetch_failed"}


def test_subscriptions_get_default_skips_sync(main_module, client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        main_module,
        "sync_jellyfin_favorite_people_to_subscriptions",
        lambda: calls.append("favorite_people") or {},
    )
    monkeypatch.setattr(
        main_module,
        "sync_jellyfin_favorite_movie_people_to_subscriptions",
        lambda: calls.append("favorite_movie_people") or {},
    )

    response = client.get("/api/subscriptions")
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert calls == []


def test_subscriptions_get_explicit_sync_runs(main_module, client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        main_module,
        "sync_jellyfin_favorite_people_to_subscriptions",
        lambda: calls.append("favorite_people") or {},
    )
    monkeypatch.setattr(
        main_module,
        "sync_jellyfin_favorite_movie_people_to_subscriptions",
        lambda: calls.append("favorite_movie_people") or {},
    )

    response = client.get("/api/subscriptions", params={"sync": "true"})
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert sorted(calls) == ["favorite_movie_people", "favorite_people"]


def test_jellyfin_library_items_degraded_on_failure():
    import jellyfin_api

    client = jellyfin_api.JellyfinClient.__new__(jellyfin_api.JellyfinClient)
    client.host = "http://jellyfin.local"
    client.api_key = "key"
    client._discover_user_id = lambda: "uid"
    client._get_headers = lambda: {}

    import requests

    class _Resp:
        status_code = 503

    original_get = requests.get
    requests.get = lambda *args, **kwargs: _Resp()
    try:
        result = client.get_library_items()
    finally:
        requests.get = original_get

    assert result["items"] == []
    assert result["total"] == 0
    assert result["degraded"] is True
    assert result["message"] == "jellyfin_unavailable"
