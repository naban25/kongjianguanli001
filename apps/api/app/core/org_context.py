"""Active organization context resolution.

为支持多组织（multi-tenant）隔离，从请求头或 cookie 中解析当前会话所选的 Organization。

优先顺序：
1. HTTP 头 `X-Organization-Id` —— API 调用方直接指定（高优先级）
2. Cookie `active_org_id`    —— 浏览器用户的最近选择
3. 数据库首个 active Organization —— 兜底
任何层级若与该用户无可见性，都会被忽略并继续往下找。

可见性规则：
- 系统管理员可访问任意 Organization（无需 membership）
- 非系统管理员仅可访问其在 ``OrganizationMembership`` 中存在记录的组织集合
  （回填后等价于通过 UserProjectRole 派生的集合，但 membership 是组织权限的源头）

解析后写入：
- `request.state.active_org_id` (uuid.UUID)
- `request.state.user_org_access` (set[uuid.UUID])
- `request.state.user_org_roles` (dict[uuid.UUID, OrgMemberRole])   # 用户在各 org 下的角色
- ContextVar `active_org_ctx` / `user_org_access_ctx` / `user_org_roles_ctx`
"""
from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar
from typing import Optional

from fastapi import HTTPException, Request, status
from sqlalchemy.orm import Session

from ..models import (
    AppUser,
    Organization,
    OrganizationMembership,
    OrganizationStatus,
    OrgMemberRole,
    Project,
)

logger = logging.getLogger(__name__)

HEADER_ORG_ID = "X-Organization-Id"
COOKIE_ORG_ID = "active_org_id"

# ContextVar: 当前请求解析出的 active organization_id
# 在 FastAPI 端点无 Request 参数时使用这里
active_org_ctx: ContextVar[Optional[uuid.UUID]] = ContextVar("active_org_ctx", default=None)
user_org_access_ctx: ContextVar[Optional[set]] = ContextVar("user_org_access_ctx", default=None)
# 用户在每个可见 org 下的 OrgMemberRole 映射（admin 视角下为空 dict：默认 admin）
user_org_roles_ctx: ContextVar[Optional[dict]] = ContextVar("user_org_roles_ctx", default=None)


def _uuid_or_none(value: Optional[str]) -> Optional[uuid.UUID]:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


def _list_user_org_ids(db: Session, user: AppUser) -> set[uuid.UUID]:
    """返回用户可见的所有 Organization ID 集合。"""
    if user.is_system_admin:
        rows = db.query(Organization.id).filter(Organization.status == OrganizationStatus.active).all()
        return {r[0] for r in rows}
    rows = (
        db.query(OrganizationMembership.organization_id)
        .filter(OrganizationMembership.user_id == user.id)
        .distinct()
        .all()
    )
    return {r[0] for r in rows}


def _list_user_org_roles(db: Session, user: AppUser) -> dict[uuid.UUID, OrgMemberRole]:
    """返回用户在每个可见 org 下的角色映射。

    - 系统管理员返回空 dict（被视作全权）
    - 普通用户返回 {(org_id, role)}
    """
    if user.is_system_admin:
        return {}
    rows = (
        db.query(
            OrganizationMembership.organization_id,
            OrganizationMembership.role,
        )
        .filter(OrganizationMembership.user_id == user.id)
        .all()
    )
    return {oid: role for oid, role in rows}


def resolve_active_org(
    request: Request,
    user: AppUser,
    db: Session,
) -> uuid.UUID:
    """解析并返回当前请求所选的 organization_id。

    - 验证所选 org 用户确实有访问权
    - 写入 `request.state.active_org_id` / `user_org_access` / `user_org_roles`
    - 若找不到任何可用 org 抛 403
    """
    user_org_ids = _list_user_org_ids(db, user)
    if not user_org_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="当前用户未关联任何组织或项目，请联系管理员分配权限。",
        )
    user_org_roles = _list_user_org_roles(db, user)

    candidate = _uuid_or_none(request.headers.get(HEADER_ORG_ID)) or _uuid_or_none(
        request.cookies.get(COOKIE_ORG_ID)
    )

    # 校验：candidate 必须在 user_org_ids 内且 organization active
    chosen: Optional[uuid.UUID] = None
    if candidate and candidate in user_org_ids:
        org = db.query(Organization).filter(Organization.id == candidate).first()
        if org and org.status == OrganizationStatus.active:
            chosen = candidate

    if not chosen:
        # 兜底：取 user_org_ids 中第一个仍 active 的 org
        rows = (
            db.query(Organization.id)
            .filter(
                Organization.id.in_(user_org_ids),
                Organization.status == OrganizationStatus.active,
            )
            .order_by(Organization.created_at.asc())
            .all()
        )
        if rows:
            chosen = rows[0][0]
        else:
            # 最后兜底：第一个 active org（管理员场景）
            rows = (
                db.query(Organization.id)
                .filter(Organization.status == OrganizationStatus.active)
                .order_by(Organization.created_at.asc())
                .all()
            )
            if rows:
                chosen = rows[0][0]

    if not chosen:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="系统尚无可用 Organization。请联系管理员先创建组织。",
        )

    request.state.active_org_id = chosen
    request.state.user_org_access = user_org_ids
    request.state.user_org_roles = user_org_roles
    # 同步写入 ContextVar，让业务函数无需传递 request 参数也能读取
    active_org_ctx.set(chosen)
    user_org_access_ctx.set(user_org_ids)
    user_org_roles_ctx.set(user_org_roles)
    return chosen


def get_active_org_id(request: Request, user: AppUser, db: Session) -> uuid.UUID:
    """便捷函数：若 request.state 中没有就重新解析一次。"""
    cached = getattr(request.state, "active_org_id", None)
    if isinstance(cached, uuid.UUID):
        return cached
    return resolve_active_org(request, user, db)


def user_can_access_org(user: AppUser, org_ids: set[uuid.UUID]) -> bool:
    """用于内部手动校验：用户能访问 org_ids 中的任一。"""
    if user.is_system_admin:
        return True
    state_set = set(org_ids)
    return bool(state_set & (getattr(user, "_user_org_access_cache", set()) or set()))


def ensure_project_in_active_org(
    project_id: str | uuid.UUID,
    user: AppUser,
    db: Session,
) -> uuid.UUID:
    """校验项目属于当前激活的组织，否则 403。

    - 系统管理员直接放行（仍可访问任意组织下的项目）
    - 普通用户必须先解析 active_org，再校验该项目的 organization_id == active_org_id

    调用时机：在 `require_project_scope` 之后、单条 SQL 之前。
    返回项目对应的 organization_id 便于调用方写入新建记录的 organization_id 列。

    实现：active_org 优先从 ContextVar / request.state 读取，由 `CurrentUser` 依赖
    自动注入（无需在每个端点接受 Request 参数）。
    """
    try:
        pid = uuid.UUID(str(project_id)) if not isinstance(project_id, uuid.UUID) else project_id
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="无效的项目 ID。")

    project = db.query(Project).filter(Project.id == pid).first()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在。")

    if user.is_system_admin:
        return project.organization_id

    # 解出 active_org：从 ContextVar 或 request.state 读
    active_org_id = active_org_ctx.get()
    accessible = user_org_access_ctx.get()
    if active_org_id is None:
        # ContextVar 未设置：可能 CurrentUser 尚未注入/内部调用场景
        accessible = _list_user_org_ids(db, user)
        if not accessible:
            raise HTTPException(status_code=403, detail="用户未关联任何组织。")
        if project.organization_id not in accessible:
            raise HTTPException(status_code=403, detail="无权访问该组织下的项目。")
        return project.organization_id

    if project.organization_id != active_org_id:
        raise HTTPException(
            status_code=403,
            detail="当前激活的组织与目标项目所属组织不一致，请先切换组织。",
        )
    return project.organization_id
