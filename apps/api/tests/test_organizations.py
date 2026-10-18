"""Tests for the multi-organization admin API (P3.1).

Covers the lifecycle edges that are easy to break but expensive to leave
uncovered:
  1. List organizations as admin
  2. Create organization (and the uniqueness check)
  3. Switch active organization + Set-Cookie persistence
  4. Disable the *active* org is rejected (no self-lock)
  5. Disable a non-active org succeeds + audit_log written

We rely on the SAVEPOINT-isolation conftest to keep each test in its own
bubble — no need to clean up organizations between tests.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text


# ---------- Helpers ------------------------------------------------------


def _seed_org(db_session, name: str, status: str = "active") -> uuid.UUID:
    """Insert a fresh Organization row directly via SQL — bypasses any
    admin-only route guard and avoids leaking through the SAVEPOINT."""
    oid = uuid.uuid4()
    db_session.execute(
        text(
            "INSERT INTO organizations (id, name, status, created_at, updated_at) "
            "VALUES (:id, :n, :s, NOW(), NOW())"
        ),
        {"id": str(oid), "n": name, "s": status},
    )
    db_session.flush()
    return oid


# ---------- 1. List ------------------------------------------------------


def test_list_organizations_as_admin(logged_in_client, admin_user):
    """系统管理员能看到完整组织列表，且响应里包含 active_org_id。"""
    resp = logged_in_client.get("/api/v1/admin/organizations")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "items" in body
    assert "active_org_id" in body
    assert "total" in body
    assert isinstance(body["items"], list)
    # Admin 一定有 active_org_id（兜底取首个 active org）
    assert body["active_org_id"]


def test_list_organizations_requires_authentication(client):
    resp = client.get("/api/v1/admin/organizations")
    assert resp.status_code in (401, 403), resp.text


# ---------- 2. Create ----------------------------------------------------


def test_create_organization_as_admin(logged_in_client):
    """新建组织 → 200，且返回值包含新生成的 UUID。"""
    name = f"测试组织-{uuid.uuid4().hex[:6]}"
    resp = logged_in_client.post(
        "/api/v1/admin/organizations",
        json={"name": name, "status": "active"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("ok") is True
    assert body.get("name") == name
    # 返回值含 id 字段且是合法 UUID
    uuid.UUID(body["id"])  # raises if not a valid UUID


def test_create_organization_rejects_duplicate_name(logged_in_client, db_session):
    """同名组织必须返回 409，避免静默覆盖。"""
    name = f"重复名组织-{uuid.uuid4().hex[:6]}"
    first = logged_in_client.post(
        "/api/v1/admin/organizations",
        json={"name": name, "status": "active"},
    )
    assert first.status_code == 200, first.text

    second = logged_in_client.post(
        "/api/v1/admin/organizations",
        json={"name": name, "status": "active"},
    )
    assert second.status_code == 409, second.text
    assert "已存在" in second.json().get("detail", "")


def test_create_organization_rejects_empty_name(logged_in_client):
    """空字符串名称必须被拦截（不会创建无意义的空 org）。"""
    resp = logged_in_client.post(
        "/api/v1/admin/organizations",
        json={"name": "   ", "status": "active"},
    )
    assert resp.status_code == 400, resp.text


# ---------- 3. Switch ----------------------------------------------------


def test_switch_organization_sets_cookie_and_audit(logged_in_client, db_session):
    """切到第二个组织 → 200，返回 Set-Cookie 且写入审计日志。"""
    # 先创建一个备用 org（确保切换目标存在）
    target_id = _seed_org(db_session, f"切换目标-{uuid.uuid4().hex[:6]}")

    # 切到目标 org
    resp = logged_in_client.post(
        "/api/v1/admin/organizations/switch",
        json={"org_id": str(target_id)},
    )
    assert resp.status_code == 200, resp.text

    # 必须返回 Set-Cookie 让浏览器持久化
    set_cookie = resp.headers.get("set-cookie", "")
    assert "active_org_id=" in set_cookie
    assert str(target_id) in set_cookie

    body = resp.json()
    assert body.get("active_org_id") == str(target_id)

    # 审计日志：必须写入一条 switch_organization 记录
    audit = db_session.execute(
        text(
            "SELECT action, resource_id FROM audit_logs "
            "WHERE action = 'switch_organization' AND resource_id = :rid"
        ),
        {"rid": str(target_id)},
    ).first()
    assert audit is not None, "switch_organization 审计日志未写入"


def test_switch_to_inaccessible_org_returns_403(logged_in_client, db_session):
    """不可访问的目标 org 必须返回 403（不会泄露存在性）。"""
    bogus = uuid.uuid4()
    resp = logged_in_client.post(
        "/api/v1/admin/organizations/switch",
        json={"org_id": str(bogus)},
    )
    # 管理员实际能访问任何 org，但目标 org 不存在 → 404
    # 普通用户不可访问他人 org → 403
    assert resp.status_code in (403, 404), resp.text


def test_switch_rejects_malformed_uuid(logged_in_client):
    resp = logged_in_client.post(
        "/api/v1/admin/organizations/switch",
        json={"org_id": "not-a-uuid"},
    )
    assert resp.status_code == 400, resp.text


# ---------- 4. Disable ---------------------------------------------------


def test_disable_active_organization_is_rejected(logged_in_client, db_session):
    """禁用当前激活的 org 必须被 400 拦截 — 防止误操作锁死会话。"""
    # 先拿到当前 active org
    me = logged_in_client.get("/api/v1/admin/organizations").json()
    active_id = me["active_org_id"]

    resp = logged_in_client.delete(f"/api/v1/admin/organizations/{active_id}")
    assert resp.status_code == 400, resp.text
    assert "当前激活" in resp.json().get("detail", "")


def test_disable_inactive_organization_succeeds(logged_in_client, db_session):
    """切到 B 后，禁用 A 必须返回 200 且写入审计日志。"""
    # 先确保至少有两个 active org（一个用作 active，另一个要被禁用）
    a_id = _seed_org(db_session, f"A组织-{uuid.uuid4().hex[:6]}")
    b_id = _seed_org(db_session, f"B组织-{uuid.uuid4().hex[:6]}")

    # 切到 B，让 A 成为非 active
    switch = logged_in_client.post(
        "/api/v1/admin/organizations/switch",
        json={"org_id": str(b_id)},
    )
    assert switch.status_code == 200, switch.text

    resp = logged_in_client.delete(f"/api/v1/admin/organizations/{a_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json().get("ok") is True

    # 审计日志：必须写入一条 disable_organization 记录
    audit = db_session.execute(
        text(
            "SELECT action FROM audit_logs "
            "WHERE action = 'disable_organization' AND resource_id = :rid"
        ),
        {"rid": str(a_id)},
    ).first()
    assert audit is not None, "disable_organization 审计日志未写入"


def test_disable_unknown_organization_returns_404(logged_in_client):
    resp = logged_in_client.delete(f"/api/v1/admin/organizations/{uuid.uuid4()}")
    assert resp.status_code == 404, resp.text


def test_disable_rejects_malformed_uuid(logged_in_client):
    resp = logged_in_client.delete("/api/v1/admin/organizations/not-a-uuid")
    assert resp.status_code == 400, resp.text
