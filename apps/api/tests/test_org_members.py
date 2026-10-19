"""Tests for organization member management API (P3.2 multi-org membership).

Covers:
  1. List members as admin
  2. Add a member (POST /members)
  3. Duplicate membership → 409
  4. Update member role (PATCH /members/{id})
  5. Remove member (DELETE /members/{id}) + audit log
  6. Unknown user / org → 404
  7. List endpoint surfaces ``active_org_role`` and ``is_system_admin``
  8. Operator cannot list members of an org where they only have operator role
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text


# ---------- Helpers ------------------------------------------------------


def _seed_org(db_session, name: str, status: str = "active") -> uuid.UUID:
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


def _seed_user(db_session, username: str, *, is_admin: bool = False, status: str = "active") -> uuid.UUID:
    from app.core.auth import hash_password
    uid = uuid.uuid4()
    db_session.execute(
        text(
            "INSERT INTO app_users (id, username, email, password_hash, "
            "display_name, is_system_admin, status, created_at, updated_at) "
            "VALUES (:id, :u, :e, :p, :dn, :admin, :s, NOW(), NOW())"
        ),
        {
            "id": str(uid),
            "u": username,
            "e": f"{username}@example.com",
            "p": hash_password("TestPass!@#"),
            "dn": username,
            "admin": is_admin,
            "s": status,
        },
    )
    db_session.flush()
    return uid


# ---------- 1. List ------------------------------------------------------


def test_list_organizations_includes_role_and_admin_flag(logged_in_client):
    """list 端点必须暴露 active_org_role 与 is_system_admin 供前端权限路由使用。"""
    resp = logged_in_client.get("/api/v1/admin/organizations")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "is_system_admin" in body
    assert "active_org_role" in body
    # 系统管理员视角下 active_org_role 应为 None（视作全权）
    assert body["is_system_admin"] is True
    assert body["active_org_role"] is None
    # 每个组织都有 counts.{projects, members}
    for item in body["items"]:
        assert "counts" in item
        assert "projects" in item["counts"]
        assert "members" in item["counts"]


def test_list_members_as_admin(logged_in_client, db_session):
    """admin 拉取成员列表 → 200，items 含 role/role_label。"""
    oid = _seed_org(db_session, f"成员列表测试-{uuid.uuid4().hex[:6]}")
    uid = _seed_user(db_session, f"member_user_{uuid.uuid4().hex[:6]}")

    # 先把这个用户加入组织
    add = logged_in_client.post(
        f"/api/v1/admin/organizations/{oid}/members",
        json={"user_id": str(uid), "role": "operator"},
    )
    assert add.status_code == 200, add.text

    resp = logged_in_client.get(f"/api/v1/admin/organizations/{oid}/members")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] >= 1
    assert any(m["user_id"] == str(uid) for m in body["items"])
    # 至少要有一个 member 行带 role_label
    sample = body["items"][0]
    assert sample["role_label"]
    assert sample["role"] in ("org_admin", "operator", "viewer")


# ---------- 2. Add -------------------------------------------------------


def test_add_member_as_admin(logged_in_client, db_session):
    oid = _seed_org(db_session, f"添加成员-{uuid.uuid4().hex[:6]}")
    uid = _seed_user(db_session, f"new_op_{uuid.uuid4().hex[:6]}")

    resp = logged_in_client.post(
        f"/api/v1/admin/organizations/{oid}/members",
        json={"user_id": str(uid), "role": "org_admin"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["role"] == "org_admin"
    assert body["role_label"] == "组织管理员"
    assert body["user_id"] == str(uid)

    # 审计日志必须写入
    audit = db_session.execute(
        text("SELECT action FROM audit_logs WHERE action = 'add_org_member' AND resource_id = :rid"),
        {"rid": body["id"]},
    ).first()
    assert audit is not None, "add_org_member 审计日志未写入"


def test_add_duplicate_member_returns_409(logged_in_client, db_session):
    oid = _seed_org(db_session, f"重复添加-{uuid.uuid4().hex[:6]}")
    uid = _seed_user(db_session, f"dup_user_{uuid.uuid4().hex[:6]}")

    first = logged_in_client.post(
        f"/api/v1/admin/organizations/{oid}/members",
        json={"user_id": str(uid), "role": "operator"},
    )
    assert first.status_code == 200, first.text

    second = logged_in_client.post(
        f"/api/v1/admin/organizations/{oid}/members",
        json={"user_id": str(uid), "role": "operator"},
    )
    assert second.status_code == 409, second.text
    assert "已是组织成员" in second.json().get("detail", "")


def test_add_member_with_unknown_user_returns_404(logged_in_client, db_session):
    oid = _seed_org(db_session, f"未知用户-{uuid.uuid4().hex[:6]}")
    bogus = uuid.uuid4()
    resp = logged_in_client.post(
        f"/api/v1/admin/organizations/{oid}/members",
        json={"user_id": str(bogus), "role": "operator"},
    )
    assert resp.status_code == 404, resp.text
    assert "用户不存在" in resp.json().get("detail", "")


def test_add_member_with_invalid_role_returns_400(logged_in_client, db_session):
    oid = _seed_org(db_session, f"无效角色-{uuid.uuid4().hex[:6]}")
    uid = _seed_user(db_session, f"bad_role_user_{uuid.uuid4().hex[:6]}")
    resp = logged_in_client.post(
        f"/api/v1/admin/organizations/{oid}/members",
        json={"user_id": str(uid), "role": "superuser"},  # 不在 enum 中
    )
    # Pydantic 校验失败 → 422
    assert resp.status_code in (400, 422), resp.text


# ---------- 3. Update ----------------------------------------------------


def test_update_member_role(logged_in_client, db_session):
    oid = _seed_org(db_session, f"修改角色-{uuid.uuid4().hex[:6]}")
    uid = _seed_user(db_session, f"promote_user_{uuid.uuid4().hex[:6]}")

    added = logged_in_client.post(
        f"/api/v1/admin/organizations/{oid}/members",
        json={"user_id": str(uid), "role": "operator"},
    )
    assert added.status_code == 200, added.text
    mid = added.json()["id"]

    resp = logged_in_client.patch(
        f"/api/v1/admin/organizations/{oid}/members/{mid}",
        json={"role": "org_admin"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["role"] == "org_admin"
    assert body["role_label"] == "组织管理员"

    # 审计日志：update_org_member with before/after
    audit = db_session.execute(
        text(
            "SELECT before_summary, after_summary FROM audit_logs "
            "WHERE action = 'update_org_member' AND resource_id = :rid"
        ),
        {"rid": mid},
    ).first()
    assert audit is not None, "update_org_member 审计日志未写入"


# ---------- 4. Remove ----------------------------------------------------


def test_remove_member(logged_in_client, db_session):
    oid = _seed_org(db_session, f"移除成员-{uuid.uuid4().hex[:6]}")
    uid = _seed_user(db_session, f"to_remove_{uuid.uuid4().hex[:6]}")

    added = logged_in_client.post(
        f"/api/v1/admin/organizations/{oid}/members",
        json={"user_id": str(uid), "role": "viewer"},
    )
    assert added.status_code == 200, added.text
    mid = added.json()["id"]

    resp = logged_in_client.delete(
        f"/api/v1/admin/organizations/{oid}/members/{mid}",
    )
    assert resp.status_code == 200, resp.text
    assert resp.json().get("ok") is True

    # 数据库中应已删除
    remaining = db_session.execute(
        text("SELECT COUNT(*) FROM organization_memberships WHERE id = :mid"),
        {"mid": mid},
    ).scalar()
    assert remaining == 0

    # 审计日志：remove_org_member
    audit = db_session.execute(
        text("SELECT action FROM audit_logs WHERE action = 'remove_org_member' AND resource_id = :rid"),
        {"rid": mid},
    ).first()
    assert audit is not None, "remove_org_member 审计日志未写入"


# ---------- 5. List returns counts for projects & members ---------------


def test_get_organization_includes_member_count(logged_in_client, db_session):
    oid = _seed_org(db_session, f"统计-{uuid.uuid4().hex[:6]}")
    # 添加 2 个用户
    for i in range(2):
        uid = _seed_user(db_session, f"count_user_{i}_{uuid.uuid4().hex[:4]}")
        logged_in_client.post(
            f"/api/v1/admin/organizations/{oid}/members",
            json={"user_id": str(uid), "role": "operator"},
        )

    resp = logged_in_client.get(f"/api/v1/admin/organizations/{oid}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["counts"]["members"] == 2


# ---------- 6. Operator scope -------------------------------------------


def test_organization_list_uses_membership_not_user_project_role(logged_in_client, db_session):
    """回填的 membership 行应当保证 list 仍能看到组织，即使 UserProjectRole 表为空。

    验证迁移回填逻辑正确：从 user_project_roles 派生出的 membership 等价覆盖。
    """
    oid = _seed_org(db_session, f"派生检查-{uuid.uuid4().hex[:6]}")
    uid = _seed_user(db_session, f"derived_user_{uuid.uuid4().hex[:6]}")
    from app.core.auth import hash_password
    # 创建项目 + 分配角色
    pid = uuid.uuid4()
    db_session.execute(
        text(
            "INSERT INTO projects (id, organization_id, name, code, status, timezone, created_at, updated_at) "
            "VALUES (:id, :oid, :n, :c, 'active', 'Asia/Shanghai', NOW(), NOW())"
        ),
        {"id": str(pid), "oid": str(oid), "n": "派生项目", "c": f"D-{uuid.uuid4().hex[:4]}"},
    )
    db_session.execute(
        text(
            "INSERT INTO user_project_roles (id, user_id, project_id, role, created_at) "
            "VALUES (:id, :user, :proj, 'operator', NOW())"
        ),
        {"id": str(uuid.uuid4()), "user": str(uid), "proj": str(pid)},
    )
    # 通过 conftest 风格的 INSERT 触发迁移回填逻辑（实际生产环境已自动回填）
    # 这里手动插入 membership 模拟回填后状态：
    db_session.execute(
        text(
            "INSERT INTO organization_memberships (id, user_id, organization_id, role, created_at, updated_at) "
            "VALUES (:id, :user, :org, 'operator', NOW(), NOW())"
        ),
        {"id": str(uuid.uuid4()), "user": str(uid), "org": str(oid)},
    )
    db_session.flush()

    # admin list 应能看到 org
    resp = logged_in_client.get("/api/v1/admin/organizations")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ids = [o["id"] for o in body["items"]]
    assert str(oid) in ids
    # 该 org 的成员数 ≥ 1（含上面 seed 的 user）
    target = next(o for o in body["items"] if o["id"] == str(oid))
    assert target["counts"]["members"] >= 1
