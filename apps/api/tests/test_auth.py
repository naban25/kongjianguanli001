"""Happy-path tests for the auth router (/api/v1/auth/*).

The dev-and-staging PG `app_users` is hit directly (we only roll back the
SAVEPOINT in the per-test fixture, so production data is untouched).
"""
import pytest

from tests.conftest import assert_ok


def test_login_with_correct_credentials_returns_token_and_sets_cookie(client, admin_user):
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": admin_user["username"], "password": admin_user["password"]},
    )
    body = assert_ok(resp)
    assert body["user"]["username"] == admin_user["username"]
    assert body["user"]["email"] == admin_user["email"]
    assert "id" in body["user"]
    assert "access_token" in body and body["access_token"]
    assert body["token_type"] == "bearer"
    # The response must include Set-Cookie for the bearer token
    set_cookie = resp.headers.get("set-cookie", "")
    assert set_cookie != ""


def test_login_with_wrong_password_returns_401(client, admin_user):
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": admin_user["username"], "password": "wrong_password"},
    )
    assert resp.status_code in (401, 400), resp.text


def test_login_with_unknown_username_returns_401(client):
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": "no_such_user_xyz", "password": "whatever"},
    )
    assert resp.status_code in (401, 400), resp.text


def test_login_with_missing_fields_returns_422(client):
    resp = client.post("/api/v1/auth/login", json={"username": "test_admin"})
    assert resp.status_code == 422, resp.text


def test_me_returns_user_when_authenticated(logged_in_client, admin_user):
    resp = logged_in_client.get("/api/v1/auth/me")
    body = assert_ok(resp)
    assert body["username"] == admin_user["username"]
    assert body["email"] == admin_user["email"]


def test_me_requires_authentication(client):
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code in (401, 403), resp.text


def test_logout_clears_session(logged_in_client):
    resp = logged_in_client.post("/api/v1/auth/logout")
    assert resp.status_code in (200, 204), resp.text

    # Subsequent call to /auth/me should fail because cookie was cleared
    # and Authorization header set by the fixture is removed.
    logged_in_client.headers.pop("Authorization", None)
    follow = logged_in_client.get("/api/v1/auth/me")
    assert follow.status_code in (401, 403), follow.text


def test_health_endpoint_is_reachable(client):
    resp = client.get("/health")
    assert resp.status_code == 200, resp.text
    assert resp.json().get("status") == "ok"
