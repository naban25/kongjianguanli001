"""User, project and role management routes (admin + project_admin)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...core.auth import CurrentUser, hash_password
from ...core.database import get_db
from ...models import (
    AppUser,
    AppUserStatus,
    AuditLog,
    Organization,
    Project,
    UserProjectRole,
    UserRole,
)

router = APIRouter(prefix="/admin", tags=["admin"])


# ---------- Schemas ----------

class ProjectAssignment(BaseModel):
    project_id: str
    role: str = Field(pattern="^(project_admin|operator)$")


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=2, max_length=128)
    email: Optional[str] = None
    password: str = Field(min_length=6, max_length=256)
    display_name: Optional[str] = None
    is_system_admin: bool = False
    project_assignments: list[ProjectAssignment] = Field(default_factory=list)


class UpdateUserRequest(BaseModel):
    email: Optional[str] = None
    password: Optional[str] = Field(default=None, min_length=6, max_length=256)
    display_name: Optional[str] = None
    status: Optional[str] = None  # active | disabled


class ProjectRolePayload(BaseModel):
    assignments: list[ProjectAssignment]


# ---------- Helpers ----------

def _enforce_system_admin(user: AppUser) -> None:
    if not user.is_system_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅系统管理员可执行此操作。")


def _audit(db: Session, req: Request, user_id: uuid.UUID, action: str, resource_type: str, resource_id: str | None = None):
    db.add(
        AuditLog(
            actor_user_id=user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            request_id=getattr(req.state, "request_id", None),
        )
    )


# ---------- Endpoints ----------

@router.get("/users")
def list_users(req: Request, db: Session = Depends(get_db), user: AppUser = Depends(CurrentUser())):
    _enforce_system_admin(user)
    rows = db.query(AppUser).order_by(AppUser.created_at.desc()).all()
    return {
        "items": [
            {
                "id": str(u.id),
                "username": u.username,
                "email": u.email,
                "display_name": u.display_name,
                "is_system_admin": u.is_system_admin,
                "status": u.status.value if hasattr(u.status, "value") else str(u.status),
                "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
                "created_at": u.created_at.isoformat(),
            }
            for u in rows
        ]
    }


@router.post("/users")
def create_user(payload: CreateUserRequest, req: Request, db: Session = Depends(get_db), user: AppUser = Depends(CurrentUser())):
    _enforce_system_admin(user)

    # Check uniqueness
    existing = db.query(AppUser).filter(AppUser.username == payload.username).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="用户名已存在。")

    if payload.email:
        existing_email = db.query(AppUser).filter(AppUser.email == payload.email).first()
        if existing_email:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="邮箱已存在。")

    # Use admin's org for simplicity
    org = db.query(Organization).first()
    new_user = AppUser(
        id=uuid.uuid4(),
        username=payload.username,
        email=payload.email,
        password_hash=hash_password(payload.password),
        display_name=payload.display_name,
        is_system_admin=payload.is_system_admin,
        status=AppUserStatus.active,
    )
    db.add(new_user)
    db.flush()

    # Project assignments
    for a in payload.project_assignments:
        try:
            pid = uuid.UUID(a.project_id)
        except ValueError:
            continue
        proj = db.query(Project).filter(Project.id == pid).first()
        if proj:
            db.add(
                UserProjectRole(
                    id=uuid.uuid4(),
                    user_id=new_user.id,
                    project_id=pid,
                    role=UserRole(a.role),
                )
            )

    _audit(db, req, user.id, "create_user", "app_user", str(new_user.id))
    db.commit()
    return {"id": str(new_user.id), "ok": True}


@router.patch("/users/{user_id}")
def update_user(
    user_id: str,
    payload: UpdateUserRequest,
    req: Request,
    db: Session = Depends(get_db),
    actor: AppUser = Depends(CurrentUser()),
):
    _enforce_system_admin(actor)

    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的用户 ID。")

    target = db.query(AppUser).filter(AppUser.id == uid).first()
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在。")

    if payload.email is not None:
        target.email = payload.email
    if payload.display_name is not None:
        target.display_name = payload.display_name
    if payload.password is not None:
        target.password_hash = hash_password(payload.password)
    if payload.status is not None:
        try:
            target.status = AppUserStatus(payload.status)
        except ValueError:
            raise HTTPException(status_code=400, detail="无效的状态值。")

    _audit(db, req, actor.id, "update_user", "app_user", user_id)
    db.commit()
    return {"ok": True}


@router.get("/projects")
def list_projects(
    db: Session = Depends(get_db),
    user: AppUser = Depends(CurrentUser()),
    all_orgs: bool = Query(False, description="系统管理员专用：返回全部组织下的项目（忽略 active_org）"),
):
    """列出当前用户可见的项目。

    - 默认按 active_org_id 过滤（非管理员强一致，管理员亦仅看激活组织下的项目）
    - 仅当 `all_orgs=true` 且调用者为系统管理员时，返回跨全部组织的项目
    """
    active_org_id = None
    if not all_orgs:
        # 仅当非管理员 / 非跨组织查询时执行 active_org 解析
        from ...core.org_context import active_org_ctx, resolve_active_org
        # 用 FastAPI request 重新解析：通过依赖注入 CurrentUser 已注入 ContextVar
        active_org_id = active_org_ctx.get()

    if all_orgs and user.is_system_admin:
        projects = db.query(Project).all()
    elif user.is_system_admin and active_org_id is None:
        projects = db.query(Project).all()
    elif user.is_system_admin and active_org_id is not None:
        # 系统管理员：返回 active_org 下的所有项目
        projects = (
            db.query(Project)
            .filter(Project.organization_id == active_org_id)
            .all()
        )
    else:
        from ...models import UserProjectRole
        q = db.query(Project).join(UserProjectRole, UserProjectRole.project_id == Project.id)
        q = q.filter(UserProjectRole.user_id == user.id)
        if active_org_id is not None:
            q = q.filter(Project.organization_id == active_org_id)
        projects = q.all()

    return {
        "items": [
            {
                "id": str(p.id),
                "name": p.name,
                "code": p.code,
                "status": p.status.value if hasattr(p.status, "value") else str(p.status),
                "timezone": p.timezone,
                "organization_id": str(p.organization_id),
            }
            for p in projects
        ]
    }


@router.get("/projects/{project_id}")
def get_project(project_id: str, db: Session = Depends(get_db), user: AppUser = Depends(CurrentUser())):
    from ...core.auth import require_project_scope
    from ...core.org_context import ensure_project_in_active_org

    try:
        pid = uuid.UUID(project_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的项目 ID。")

    proj = db.query(Project).filter(Project.id == pid).first()
    if not proj:
        raise HTTPException(status_code=404, detail="项目不存在。")

    if not require_project_scope(project_id, user, db):
        raise HTTPException(status_code=403, detail="无权访问此项目。")

    # 组织作用域校验（确保非管理员无法跨组织访问）
    ensure_project_in_active_org(project_id, user, db)

    return {
        "id": str(proj.id),
        "name": proj.name,
        "code": proj.code,
        "timezone": proj.timezone,
        "status": proj.status.value if hasattr(proj.status, "value") else str(proj.status),
        "organization_id": str(proj.organization_id),
    }


@router.put("/users/{user_id}/project-roles")
def assign_project_roles(
    user_id: str,
    payload: ProjectRolePayload,
    req: Request,
    db: Session = Depends(get_db),
    actor: AppUser = Depends(CurrentUser()),
):
    _enforce_system_admin(actor)

    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的用户 ID。")

    target = db.query(AppUser).filter(AppUser.id == uid).first()
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在。")

    # Reset then re-assign
    db.query(UserProjectRole).filter(UserProjectRole.user_id == uid).delete()
    for a in payload.assignments:
        try:
            pid = uuid.UUID(a.project_id)
        except ValueError:
            continue
        db.add(
            UserProjectRole(
                id=uuid.uuid4(),
                user_id=uid,
                project_id=pid,
                role=UserRole(a.role),
            )
        )

    _audit(db, req, actor.id, "assign_project_roles", "app_user", user_id)
    db.commit()
    return {"ok": True}
