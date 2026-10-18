"""Pytest configuration — fixtures and test-db setup.

Test strategy
-------------
We reuse the project's real PostgreSQL database (so all production schema is
hit as-is) but isolate each test in a SAVEPOINT that is rolled back on
teardown. This gives every test a clean state without the overhead of running
alembic migrations per test.

The FastAPI ``get_db`` dependency is overridden to use the same session as
the per-test fixture, so any side-effects from request handlers are also
reverted when the test exits.

The APScheduler lifespan hook is bypassed entirely — it would otherwise take
the fcntl scheduler lock, fire mock jobs and pollute logs. None of the
asserted paths depend on scheduler background activity; they hit the
``/admin/scheduler/*`` endpoints directly.
"""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, Iterator

# Ensure tests do not require an external LLM — we route to rule-based fallback
os.environ.setdefault("LLM_PROVIDER", "")
os.environ.setdefault("LLM_API_KEY", "")
os.environ.setdefault("LLM_BASE_URL", "")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event as sa_event
from sqlalchemy import text

from app.core.database import SessionLocal, engine
from app.main import app
from app.core.database import get_db


# --- Disable APScheduler lifespan for tests -------------------------------

@asynccontextmanager
async def _noop_lifespan(_app):
    yield


app.router.lifespan_context = _noop_lifespan


# --- DB session fixture with nested transaction isolation -----------------

@pytest.fixture()
def db_session() -> Iterator:
    """Yield a SQLAlchemy session bound to a SAVEPOINT that gets rolled back.

    Uses the project's real engine. The outer transaction is created on a
    dedicated connection so the SAVEPOINT is fully isolated from any other
    worker / process.
    """
    connection = engine.connect()
    transaction = connection.begin()
    session = SessionLocal(bind=connection)
    # use a nested SAVEPOINT so multiple commits inside the test get rolled back
    nested = connection.begin_nested()

    @sa_event.listens_for(session, "after_transaction_end")
    def _restart_savepoint(_session, _transaction):
        nonlocal nested
        if not nested.is_active:
            nested = connection.begin_nested()

    try:
        yield session
    finally:
        try:
            if nested.is_active:
                nested.rollback()
            if transaction.is_active:
                transaction.rollback()
        finally:
            session.close()
            connection.close()


# --- Test client with overridden get_db -----------------------------------

@pytest.fixture()
def client(db_session) -> Iterator[TestClient]:
    """Yield a FastAPI TestClient that shares the per-test session."""

    def _override_get_db():
        try:
            yield db_session
        finally:
            # do not close here — session lifecycle belongs to db_session
            pass

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# --- Lightweight seed fixtures --------------------------------------------

@pytest.fixture()
def admin_user(db_session) -> dict:
    """Create an active admin user directly via SQL (idempotent).

    Also seeds a default active Organization so that resolve_active_org()
    can find at least one active org for the admin — required by
    /admin/organizations/* and other active-org-scoped endpoints.
    """
    from app.core.auth import hash_password

    user_id = uuid.uuid4()
    pwd = hash_password("TestPass!@#")
    db_session.execute(
        text(
            "INSERT INTO app_users (id, username, email, password_hash, "
            "display_name, is_system_admin, status, created_at, updated_at) "
            "VALUES (:id, :u, :e, :p, :dn, true, 'active', NOW(), NOW())"
        ),
        {"id": str(user_id), "u": "test_admin", "e": "test_admin@example.com",
         "p": pwd, "dn": "Test Admin"},
    )
    # 默认组织：admin 必有一个可见 org，避免 CI 全新 DB 触发 403
    # organizations 表对 name 没有 unique 约束，用 SELECT first 防重复
    existing = db_session.execute(
        text("SELECT id FROM organizations WHERE name = :n LIMIT 1"),
        {"n": "Test Default Org"},
    ).first()
    if existing is None:
        db_session.execute(
            text(
                "INSERT INTO organizations (id, name, status, created_at, updated_at) "
                "VALUES (:oid, :n, 'active', NOW(), NOW())"
            ),
            {"oid": str(uuid.uuid4()), "n": "Test Default Org"},
        )
    db_session.flush()
    return {
        "id": user_id,
        "username": "test_admin",
        "email": "test_admin@example.com",
        "password": "TestPass!@#",
    }


@pytest.fixture()
def logged_in_client(client, admin_user) -> TestClient:
    """Log the admin user in via /auth/login and return the TestClient with
    the auth cookie attached.

    Test environment uses HTTP (not HTTPS), so the cookie's ``Secure`` flag
    is ignored by httpx TestClient. We also capture the JWT from the response
    body and set it on the client as a default ``Authorization`` header to
    keep test assertions robust regardless of cookie handling.
    """
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": admin_user["username"], "password": admin_user["password"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    token = body.get("access_token")
    if token:
        client.headers["Authorization"] = f"Bearer {token}"
    return client


# --- Convenience: assert-200 helper --------------------------------------

def assert_ok(resp, expected_status: int = 200) -> dict:
    assert resp.status_code == expected_status, (
        f"Expected {expected_status} but got {resp.status_code}: {resp.text}"
    )
    try:
        return resp.json()
    except Exception:
        return {}


# Re-export so tests can `from tests.conftest import assert_ok`
__all__ = [
    "pytest",
    "assert_ok",
]
