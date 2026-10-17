"""Organization management routes (super-admin only)."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...core.auth import ActiveContext, ActiveOrg
from ...core.database import get_db
from ...models import (
    AppUser,
    AuditLog,
    Organization,
    OrganizationStatus,
    Project,
    ProjectStatus,
    UserProjectRole,
    UserRole,
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


# ---------- Helpers ----------

def _require_admin(ctx: ActiveContext) -> None:
    if not ctx.user.is_system_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅系统管理员可访问。")


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


# ---------- Endpoints ----------

@router.get("")
def list_organizations(
    db: Session = Depends(get_db),
    ctx: ActiveContext = Depends(ActiveOrg()),
) -> dict[str, Any]:
    """列出当前用户可见的全部 Organization。

    - 系统管理员：全部
    - 普通用户：基于 UserProjectRole 推导的 organization 集合
    """
    if ctx.user.is_system_admin:
        rows = db.query(Organization).order_by(Organization.created_at.asc()).all()
    else:
        from sqlalchemy import select
        # 通过 UserProjectRole → Project.organization_id 聚合
        org_id_subq = (
            db.query(Project.organization_id)
            .join(UserProjectRole, UserProjectRole.project_id == Project.id)
            .filter(UserProjectRole.user_id == ctx.user.id)
            .distinct()
            .subquery()
        )
        rows = (
            db.query(Organization)
            .filter(Organization.id.in_(db.query(org_id_subq)))
            .order_by(Organization.created_at.asc())
            .all()
        )

    # 单条 SQL 汇总每个 org 的 project 数与 user 数（系统管理员场景用）
    project_counts: dict[str, int] = {}
    if rows:
        from sqlalchemy import func
        cnt_rows = (
            db.query(Project.organization_id, func.count(Project.id))
            .filter(Project.organization_id.in_([r.id for r in rows]))
            .group_by(Project.organization_id)
            .all()
        )
        project_counts = {str(oid): cnt for oid, cnt in cnt_rows}

    return {
        "items": [
            _serialize(o, counts={"projects": project_counts.get(str(o.id), 0)})
            for o in rows
        ],
        "total": len(rows),
        "active_org_id": str(ctx.active_org_id),
        "accessible_org_ids": [str(x) for x in sorted(ctx.accessible_org_ids)],
    }


@router.post("")
def create_organization(
    payload: OrganizationCreate,
    req: Request,
    db: Session = Depends(get_db),
    ctx: ActiveContext = Depends(ActiveOrg()),
) -> dict[str, Any]:
    _require_admin(ctx)

    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="组织名称不能为空。")

    # 唯一性（忽略大小写）
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
    _require_admin(ctx)
    try:
        oid = uuid.UUID(org_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的组织 ID。")
    org = db.query(Organization).filter(Organization.id == oid).first()
    if not org:
        raise HTTPException(status_code=404, detail="组织不存在。")

    # 统计下属项目数
    from sqlalchemy import func
    project_count = (
        db.query(func.count(Project.id))
        .filter(Project.organization_id == oid)
        .scalar()
    ) or 0

    return _serialize(org, counts={"projects": int(project_count)})


@router.patch("/{org_id}")
def update_organization(
    org_id: str,
    payload: OrganizationUpdate,
    req: Request,
    db: Session = Depends(get_db),
    ctx: ActiveContext = Depends(ActiveOrg()),
) -> dict[str, Any]:
    _require_admin(ctx)
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
        # 唯一性
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
    """逻辑删除：仅置 status=disabled，保留数据用于审计追溯。"""
    _require_admin(ctx)
    try:
        oid = uuid.UUID(org_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的组织 ID。")

    org = db.query(Organization).filter(Organization.id == oid).first()
    if not org:
        raise HTTPException(status_code=404, detail="组织不存在。")

    # 防止误禁用当前激活的 org（避免把会话锁死）
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


@router.post("/switch")
def switch_active_organization(
    payload: OrganizationSwitchPayload,
    req: Request,
    db: Session = Depends(get_db),
    ctx: ActiveContext = Depends(ActiveOrg()),
):
    """校验目标 org 是否在用户可见集合中，返回 Set-Cookie 提示给浏览器。

    浏览器侧需要在收到响应后手动调用 Set-Cookie 写入 `active_org_id`，
    或前端在切到 Next.js 路由时同步设置 cookie 后整页刷新。
    """
    try:
        oid = uuid.UUID(payload.org_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的组织 ID。")

    if oid not in ctx.accessible_org_ids:
        raise HTTPException(status_code=403, detail="无权访问所选组织。")

    org = db.query(Organization).filter(Organization.id == oid).first()
    if not org or org.status != OrganizationStatus.active:
        raise HTTPException(status_code=404, detail="目标组织不可用。")

    # 记录切换事件到审计日志
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
    # 通过 Set-Cookie 头通知浏览器持久化选择（1 年）
    from fastapi.responses import JSONResponse
    json_resp = JSONResponse(content=resp)
    json_resp.set_cookie(
        key="active_org_id",
        value=str(oid),
        max_age=60 * 60 * 24 * 365,
        path="/",
        samesite="lax",
        httponly=False,  # 前端 JS 需读取用于显示与下一跳请求
    )
    return json_resp
