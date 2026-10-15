import os

import pytest
from fastapi.testclient import TestClient


PROTECTED_PATH = "/api/config/system"
ADMIN_USERNAME = "auth-test-admin"
ADMIN_PASSWORD = "test-password-123"


@pytest.fixture(scope="module")
def client(main_module):
    return TestClient(main_module.app, raise_server_exceptions=False)


@pytest.fixture(scope="module", autouse=True)
def auth_test_environment(main_module):
    # backend/.env carries REQUIRE_AUTH=false for local dev; force-enable it here.
    original_require_auth = os.environ.get("REQUIRE_AUTH")
    os.environ["REQUIRE_AUTH"] = "true"
    yield
    if original_require_auth is None:
        os.environ.pop("REQUIRE_AUTH", None)
    else:
        os.environ["REQUIRE_AUTH"] = original_require_auth
    conn = main_module.db.get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM users")
        conn.commit()
    finally:
        main_module.db._release_connection(conn)


def test_open_access_when_no_users(main_module, client):
    assert not main_module.db.has_any_user()
    response = client.get(PROTECTED_PATH)
    assert response.status_code == 200
    assert response.json().get("success") is True

    status = client.get("/api/auth/status")
    assert status.status_code == 200
    assert status.json()["data"] == {"auth_required": False, "setup_required": True}


def test_setup_rejects_short_password(client):
    response = client.post("/api/auth/setup", json={"username": ADMIN_USERNAME, "password": "short"})
    assert response.status_code == 400


def test_setup_creates_first_user(main_module, client):
    response = client.post("/api/auth/setup", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["token"]
    assert main_module.db.has_any_user()

    status = client.get("/api/auth/status").json()["data"]
    assert status == {"auth_required": True, "setup_required": False}


def test_setup_second_call_forbidden(client):
    response = client.post("/api/auth/setup", json={"username": "another-user", "password": "another-password-1"})
    assert response.status_code == 403


def test_login_wrong_password(client):
    response = client.post("/api/auth/login", json={"username": ADMIN_USERNAME, "password": "wrong-password"})
    assert response.status_code == 401


def test_login_success(client):
    response = client.post("/api/auth/login", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["token"]


def test_login_rate_limited_after_five_failures(client):
    for _ in range(5):
        response = client.post("/api/auth/login", json={"username": "rate-limit-user", "password": "bad-password"})
        assert response.status_code == 401
    response = client.post("/api/auth/login", json={"username": "rate-limit-user", "password": "bad-password"})
    assert response.status_code == 429


def test_protected_endpoint_with_token(client):
    token = client.post(
        "/api/auth/login", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
    ).json()["data"]["token"]
    response = client.get(PROTECTED_PATH, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json().get("success") is True


def test_protected_endpoint_without_token(client):
    response = client.get(PROTECTED_PATH)
    assert response.status_code == 401
    assert response.json() == {"detail": "authentication_required"}


def test_protected_endpoint_with_bad_token(client):
    response = client.get(PROTECTED_PATH, headers={"Authorization": "Bearer garbage.token.signature"})
    assert response.status_code == 401
    assert response.json() == {"detail": "authentication_required"}


def test_whitelist_paths_need_no_token(client):
    assert client.get("/api/health/live").status_code == 200
    assert client.get("/api/auth/status").status_code == 200


def test_require_auth_env_disabled_bypasses(client, monkeypatch):
    monkeypatch.setenv("REQUIRE_AUTH", "false")
    response = client.get(PROTECTED_PATH)
    assert response.status_code == 200
    assert response.json().get("success") is True
