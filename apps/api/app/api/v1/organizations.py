"""Organization management routes (super-admin only)."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ...core.auth import ActiveContext, ActiveOrg
from ...core.database import get_db
from ...models import (
    AppUser,
    AppUserStatus,
    AuditLog,
    Organization,
    OrganizationMembership,
    OrganizationStatus,
    OrgMemberRole,
    Project,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/organizations", tags=["admin-organizations"])


# ---------- Schemas ----------

class OrganizationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    status: Optional[str] = Field(default="active")


class OrganizationUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    status: Optional[str] = None


class OrganizationSwitchPayload(BaseModel):
    org_id: str = Field(min_length=1, description="目标组织 UUID")


class MemberCreate(BaseModel):
    user_id: str = Field(min_length=1, description="目标用户 UUID")
    role: str = Field(default="operator", pattern="^(org_admin|operator|viewer)$")


class MemberUpdate(BaseModel):
    role: str = Field(pattern="^(org_admin|operator|viewer)$")


# ---------- Helpers ----------

def _require_admin(ctx: ActiveContext) -> None:
    """要求系统管理员或当前激活组织的 org_admin 角色。"""
    if not ctx.user.is_system_admin and not ctx.is_org_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要系统管理员或组织管理员权限。")


def _serialize(o: Organization, *, counts: dict[str, int] | None = None) -> dict[str, Any]:
    return {
        "id": str(o.id),
        "name": o.name,
        "status": o.status.value if hasattr(o.status, "value") else str(o.status),
        "created_at": o.created_at.isoformat(),
        "updated_at": o.updated_at.isoformat(),
        "counts": counts or {},
    }


def _audit(db: Session, req: Request, user_id: uuid.UUID, action: str, org_id: uuid.UUID, extra: dict[str, Any] | None = None) -> None:
    payload = {
        "actor_user_id": user_id,
        "action": action,
        "resource_type": "organization",
        "resource_id": str(org_id),
        "organization_id": org_id,
        "before_summary": extra,
        "after_summary": None,
        "request_id": getattr(req.state, "request_id", None),
    }
    db.add(AuditLog(**payload))


def _serialize_member(m: OrganizationMembership) -> dict[str, Any]:
    u = m.user
    role_val = m.role.value if hasattr(m.role, "value") else str(m.role)
    return {
        "id": str(m.id),
        "user_id": str(m.user_id),
        "username": u.username if u else None,
        "display_name": u.display_name if u else None,
        "email": u.email if u else None,
        "is_system_admin": bool(u.is_system_admin) if u else False,
        "user_status": u.status.value if (u and hasattr(u.status, "value")) else (str(u.status) if u else None),
        "role": role_val,
        "role_label": {
            OrgMemberRole.org_admin.value: "组织管理员",
            OrgMemberRole.operator.value: "运营人员",
            OrgMemberRole.viewer.value: "只读",
        }.get(role_val, role_val),
        "created_at": m.created_at.isoformat(),
        "updated_at": m.updated_at.isoformat(),
    }


# ---------- Endpoints ----------

@router.get("")
def list_organizations(
    db: Session = Depends(get_db),
    ctx: ActiveContext = Depends(ActiveOrg()),
) -> dict[str, Any]:
    """列出当前用户可见的全部 Organization。

    - 系统管理员：全部 active org
    - 普通用户：通过 OrganizationMembership 派生（迁移已自动回填，等价于项目派生）
    """
    if ctx.user.is_system_admin:
        rows = db.query(Organization).order_by(Organization.created_at.asc()).all()
    else:
        org_id_subq = (
            db.query(OrganizationMembership.organization_id)
            .filter(OrganizationMembership.user_id == ctx.user.id)
            .distinct()
            .subquery()
        )
        rows = (
            db.query(Organization)
            .filter(Organization.id.in_(db.query(org_id_subq)))
            .order_by(Organization.created_at.asc())
            .all()
        )

    # 单条 SQL 汇总每个 org 的 project 数 + member 数
    project_counts: dict[str, int] = {}
    member_counts: dict[str, int] = {}
    if rows:
        org_ids = [r.id for r in rows]
        proj_rows = (
            db.query(Project.organization_id, func.count(Project.id))
            .filter(Project.organization_id.in_(org_ids))
            .group_by(Project.organization_id)
            .all()
        )
        project_counts = {str(oid): cnt for oid, cnt in proj_rows}
        mem_rows = (
            db.query(OrganizationMembership.organization_id, func.count(OrganizationMembership.id))
            .filter(OrganizationMembership.organization_id.in_(org_ids))
            .group_by(OrganizationMembership.organization_id)
            .all()
        )
        member_counts = {str(oid): cnt for oid, cnt in mem_rows}

    return {
        "items": [
            _serialize(
                o,
                counts={
                    "projects": project_counts.get(str(o.id), 0),
                    "members": member_counts.get(str(o.id), 0),
                },
            )
            for o in rows
        ],
        "total": len(rows),
        "active_org_id": str(ctx.active_org_id),
        "accessible_org_ids": [str(x) for x in sorted(ctx.accessible_org_ids)],
        "active_org_role": (ctx.active_org_role.value if ctx.active_org_role else None),
        "is_system_admin": ctx.user.is_system_admin,
    }


@router.post("")
def create_organization(
    payload: OrganizationCreate,
    req: Request,
    db: Session = Depends(get_db),
    ctx: ActiveContext = Depends(ActiveOrg()),
) -> dict[str, Any]:
    if not ctx.user.is_system_admin:
        raise HTTPException(status_code=403, detail="仅系统管理员可创建组织。")

    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="组织名称不能为空。")

    existing = (
        db.query(Organization)
        .filter(Organization.name.ilike(name))
        .first()
    )
    if existing:
        raise HTTPException(status_code=409, detail="同名组织已存在。")

    try:
        status_val = OrganizationStatus(payload.status or "active")
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的状态值。")

    new_org = Organization(
        id=uuid.uuid4(),
        name=name,
        status=status_val,
    )
    db.add(new_org)
    db.flush()

    _audit(db, req, ctx.user.id, "create_organization", new_org.id, {"name": name})
    db.commit()
    return {
        "id": str(new_org.id),
        "name": new_org.name,
        "status": new_org.status.value if hasattr(new_org.status, "value") else str(new_org.status),
        "ok": True,
    }


@router.get("/{org_id}")
def get_organization(
    org_id: str,
    db: Session = Depends(get_db),
    ctx: ActiveContext = Depends(ActiveOrg()),
) -> dict[str, Any]:
    try:
        oid = uuid.UUID(org_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的组织 ID。")
    if oid not in ctx.accessible_org_ids:
        raise HTTPException(status_code=403, detail="无权访问此组织。")
    org = db.query(Organization).filter(Organization.id == oid).first()
    if not org:
        raise HTTPException(status_code=404, detail="组织不存在。")

    project_count = (
        db.query(func.count(Project.id))
        .filter(Project.organization_id == oid)
        .scalar()
    ) or 0
    member_count = (
        db.query(func.count(OrganizationMembership.id))
        .filter(OrganizationMembership.organization_id == oid)
        .scalar()
    ) or 0

    return _serialize(
        org,
        counts={
            "projects": int(project_count),
            "members": int(member_count),
            "buildings": 0,  # 预留字段
            "spaces": 0,     # 预留字段
        },
    )


@router.patch("/{org_id}")
def update_organization(
    org_id: str,
    payload: OrganizationUpdate,
    req: Request,
    db: Session = Depends(get_db),
    ctx: ActiveContext = Depends(ActiveOrg()),
) -> dict[str, Any]:
    if not ctx.user.is_system_admin:
        raise HTTPException(status_code=403, detail="仅系统管理员可修改组织基本信息。")
    try:
        oid = uuid.UUID(org_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的组织 ID。")

    org = db.query(Organization).filter(Organization.id == oid).first()
    if not org:
        raise HTTPException(status_code=404, detail="组织不存在。")

    before = {"id": str(org.id), "name": org.name, "status": org.status.value if hasattr(org.status, "value") else str(org.status)}
    changes: dict[str, Any] = {}

    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="组织名称不能为空。")
        dup = (
            db.query(Organization)
            .filter(Organization.id != oid, Organization.name.ilike(name))
            .first()
        )
        if dup:
            raise HTTPException(status_code=409, detail="同名组织已存在。")
        org.name = name
        changes["name"] = name
    if payload.status is not None:
        try:
            org.status = OrganizationStatus(payload.status)
        except ValueError:
            raise HTTPException(status_code=400, detail="无效的状态值。")
        changes["status"] = payload.status

    if changes:
        org.updated_at = datetime.now(timezone.utc)
        db.add(AuditLog(
            actor_user_id=ctx.user.id,
            action="update_organization",
            resource_type="organization",
            resource_id=str(oid),
            organization_id=oid,
            before_summary=before,
            after_summary=changes,
            request_id=getattr(req.state, "request_id", None),
        ))
    db.commit()
    return _serialize(org)


@router.delete("/{org_id}")
def disable_organization(
    org_id: str,
    req: Request,
    db: Session = Depends(get_db),
    ctx: ActiveContext = Depends(ActiveOrg()),
) -> dict[str, Any]:
    """逻辑删除：仅置 status=inactive，保留数据用于审计追溯。"""
    if not ctx.user.is_system_admin:
        raise HTTPException(status_code=403, detail="仅系统管理员可禁用组织。")
    try:
        oid = uuid.UUID(org_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的组织 ID。")

    org = db.query(Organization).filter(Organization.id == oid).first()
    if not org:
        raise HTTPException(status_code=404, detail="组织不存在。")

    if org.id == ctx.active_org_id:
        raise HTTPException(status_code=400, detail="无法禁用当前激活的组织，请先切换到其他组织。")

    org.status = OrganizationStatus.inactive
    org.updated_at = datetime.now(timezone.utc)

    db.add(AuditLog(
        actor_user_id=ctx.user.id,
        action="disable_organization",
        resource_type="organization",
        resource_id=str(oid),
        organization_id=oid,
        request_id=getattr(req.state, "request_id", None),
    ))
    db.commit()
    return {"ok": True, "id": str(oid)}


# ---------- Members ----------

@router.get("/{org_id}/members")
def list_org_members(
    org_id: str,
    db: Session = Depends(get_db),
    ctx: ActiveContext = Depends(ActiveOrg()),
) -> dict[str, Any]:
    """列出组织下的所有成员（系统管理员或 org_admin）。"""
    try:
        oid = uuid.UUID(org_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的组织 ID。")
    if oid not in ctx.accessible_org_ids:
        raise HTTPException(status_code=403, detail="无权访问此组织。")
    if not ctx.user.is_system_admin and ctx.active_org_role != OrgMemberRole.org_admin:
        raise HTTPException(status_code=403, detail="仅组织管理员或系统管理员可查看成员。")
    rows = (
        db.query(OrganizationMembership)
        .filter(OrganizationMembership.organization_id == oid)
        .order_by(OrganizationMembership.created_at.asc())
        .all()
    )
    return {"items": [_serialize_member(m) for m in rows], "total": len(rows)}


@router.post("/{org_id}/members")
def add_org_member(
    org_id: str,
    payload: MemberCreate,
    req: Request,
    db: Session = Depends(get_db),
    ctx: ActiveContext = Depends(ActiveOrg()),
) -> dict[str, Any]:
    """添加成员到组织。仅系统管理员或 org_admin。"""
    try:
        oid = uuid.UUID(org_id)
        uid = uuid.UUID(payload.user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的 ID。")
    if oid not in ctx.accessible_org_ids:
        raise HTTPException(status_code=403, detail="无权访问此组织。")
    if not ctx.user.is_system_admin and ctx.active_org_role != OrgMemberRole.org_admin:
        raise HTTPException(status_code=403, detail="仅组织管理员或系统管理员可管理成员。")

    org = db.query(Organization).filter(Organization.id == oid).first()
    if not org:
        raise HTTPException(status_code=404, detail="组织不存在。")
    user = db.query(AppUser).filter(AppUser.id == uid).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在。")

    # 唯一性检查
    existing = (
        db.query(OrganizationMembership)
        .filter(OrganizationMembership.user_id == uid, OrganizationMembership.organization_id == oid)
        .first()
    )
    if existing:
        raise HTTPException(status_code=409, detail="该用户已是组织成员。")

    try:
        role_val = OrgMemberRole(payload.role)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的角色。")

    m = OrganizationMembership(
        id=uuid.uuid4(),
        user_id=uid,
        organization_id=oid,
        role=role_val,
    )
    db.add(m)
    db.add(AuditLog(
        actor_user_id=ctx.user.id,
        action="add_org_member",
        resource_type="organization_member",
        resource_id=str(m.id),
        organization_id=oid,
        after_summary={"user_id": str(uid), "role": role_val.value},
        request_id=getattr(req.state, "request_id", None),
    ))
    db.commit()
    return _serialize_member(m)


@router.patch("/{org_id}/members/{member_id}")
def update_org_member(
    org_id: str,
    member_id: str,
    payload: MemberUpdate,
    req: Request,
    db: Session = Depends(get_db),
    ctx: ActiveContext = Depends(ActiveOrg()),
) -> dict[str, Any]:
    """修改成员角色。仅系统管理员或 org_admin。"""
    try:
        oid = uuid.UUID(org_id)
        mid = uuid.UUID(member_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的 ID。")
    if oid not in ctx.accessible_org_ids:
        raise HTTPException(status_code=403, detail="无权访问此组织。")
    if not ctx.user.is_system_admin and ctx.active_org_role != OrgMemberRole.org_admin:
        raise HTTPException(status_code=403, detail="仅组织管理员或系统管理员可管理成员。")

    m = (
        db.query(OrganizationMembership)
        .filter(OrganizationMembership.id == mid, OrganizationMembership.organization_id == oid)
        .first()
    )
    if not m:
        raise HTTPException(status_code=404, detail="成员不存在。")

    try:
        new_role = OrgMemberRole(payload.role)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的角色。")

    before_role = m.role.value if hasattr(m.role, "value") else str(m.role)
    m.role = new_role
    m.updated_at = datetime.now(timezone.utc)
    db.add(AuditLog(
        actor_user_id=ctx.user.id,
        action="update_org_member",
        resource_type="organization_member",
        resource_id=str(m.id),
        organization_id=oid,
        before_summary={"role": before_role},
        after_summary={"role": new_role.value},
        request_id=getattr(req.state, "request_id", None),
    ))
    db.commit()
    return _serialize_member(m)


@router.delete("/{org_id}/members/{member_id}")
def remove_org_member(
    org_id: str,
    member_id: str,
    req: Request,
    db: Session = Depends(get_db),
    ctx: ActiveContext = Depends(ActiveOrg()),
) -> dict[str, Any]:
    """从组织移除成员。仅系统管理员或 org_admin。

    注意：从当前激活组织移除 org_admin 后，其本人立刻丧失权限（该 membership 行消失），
    所以阻止把当前激活组织中唯一的 org_admin 移除。
    """
    try:
        oid = uuid.UUID(org_id)
        mid = uuid.UUID(member_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的 ID。")
    if oid not in ctx.accessible_org_ids:
        raise HTTPException(status_code=403, detail="无权访问此组织。")
    if not ctx.user.is_system_admin and ctx.active_org_role != OrgMemberRole.org_admin:
        raise HTTPException(status_code=403, detail="仅组织管理员或系统管理员可管理成员。")

    m = (
        db.query(OrganizationMembership)
        .filter(OrganizationMembership.id == mid, OrganizationMembership.organization_id == oid)
        .first()
    )
    if not m:
        raise HTTPException(status_code=404, detail="成员不存在。")

    # 防止误移除当前激活组织中唯一的 org_admin（避免锁死）
    if oid == ctx.active_org_id and m.user_id == ctx.user.id:
        if m.role == OrgMemberRole.org_admin:
            other_admins = (
                db.query(func.count(OrganizationMembership.id))
                .filter(
                    OrganizationMembership.organization_id == oid,
                    OrganizationMembership.role == OrgMemberRole.org_admin,
                    OrganizationMembership.id != mid,
                )
                .scalar()
            ) or 0
            if other_admins == 0:
                raise HTTPException(
                    status_code=400,
                    detail="当前激活组织的唯一组织管理员无法移除自己，请先指派其他组织管理员。",
                )

    db.delete(m)
    db.add(AuditLog(
        actor_user_id=ctx.user.id,
        action="remove_org_member",
        resource_type="organization_member",
        resource_id=str(mid),
        organization_id=oid,
        request_id=getattr(req.state, "request_id", None),
    ))
    db.commit()
    return {"ok": True, "id": str(mid)}


# ---------- Switch ----------

@router.post("/switch")
def switch_active_organization(
    payload: OrganizationSwitchPayload,
    req: Request,
    db: Session = Depends(get_db),
    ctx: ActiveContext = Depends(ActiveOrg()),
):
    """校验目标 org 是否在用户可见集合中，返回 Set-Cookie 提示给浏览器。"""
    try:
        oid = uuid.UUID(payload.org_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的组织 ID。")

    if oid not in ctx.accessible_org_ids:
        raise HTTPException(status_code=403, detail="无权访问所选组织。")

    org = db.query(Organization).filter(Organization.id == oid).first()
    if not org or org.status != OrganizationStatus.active:
        raise HTTPException(status_code=404, detail="目标组织不可用。")

    db.add(AuditLog(
        actor_user_id=ctx.user.id,
        action="switch_organization",
        resource_type="organization",
        resource_id=str(oid),
        organization_id=oid,
        after_summary={"from": str(ctx.active_org_id), "to": str(oid)},
        request_id=getattr(req.state, "request_id", None),
    ))
    db.commit()

    resp = {
        "ok": True,
        "active_org_id": str(oid),
        "name": org.name,
    }
    from fastapi.responses import JSONResponse
    json_resp = JSONResponse(content=resp)
    json_resp.set_cookie(
        key="active_org_id",
        value=str(oid),
        max_age=60 * 60 * 24 * 365,
        path="/",
        samesite="lax",
        httponly=False,
    )
    return json_resp
