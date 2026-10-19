"""Authentication utilities: password hashing, JWT creation/verification, current-user dependency.

Uses bcrypt directly (not passlib) to avoid passlib + bcrypt 5.x compatibility issues.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import bcrypt as _bcrypt
from jose import jwt
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..core.database import get_db
from ..models import AppUser, AppUserStatus, OrgMemberRole

from .org_context import resolve_active_org  # noqa: E402

settings = get_settings()


def hash_password(password: str) -> str:
    return _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


def create_access_token(user: AppUser) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(user.id),
        "username": user.username,
        "is_system_admin": user.is_system_admin,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=settings.jwt_expire_hours)).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any] | None:
    try:
        return jwt.decode(
            token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm]
        )
    except jwt.ExpiredSignatureError:  # type: ignore[attr-defined]
        return None
    except jwt.JWTError:  # type: ignore[attr-defined]
        return None


class CurrentUser:
    """FastAPI dependency that resolves the authenticated user from Bearer token or cookie."""

    def __init__(self, require_active: bool = True):
        self.require_active = require_active

    async def __call__(self, request: Request, db: Session = Depends(get_db)) -> AppUser:
        auth = request.headers.get("Authorization", "")
        token: Optional[str] = None
        if auth.startswith("Bearer "):
            token = auth[7:].strip()
        if not token:
            token = request.cookies.get("session")
        if not token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录或会话已过期。")

        payload = decode_access_token(token)
        if not payload:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="会话已失效，请重新登录。")

        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效的认证凭据。")

        try:
            import uuid as _uuid
            user_uuid = _uuid.UUID(user_id)
        except (ValueError, TypeError):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效的凭据格式。")

        user = db.query(AppUser).filter(AppUser.id == user_uuid).first()
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在。")

        if self.require_active and user.status != AppUserStatus.active:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="用户已被禁用。")

        request.state.current_user = user
        # 同时解析 active organization 并写入 ContextVar（多租户）
        try:
            resolve_active_org(request, user, db)
        except HTTPException:
            # 解析失败不阻断请求，让后续代码决定如何处理
            pass
        return user


def require_project_scope(project_id: str, user: AppUser, db: Session) -> bool:
    """Check whether the user has access to the given project.

    system_admin → all projects
    project_admin / operator → must have a user_project_roles row
    """
    if user.is_system_admin:
        return True
    from ..models import UserProjectRole

    try:
        import uuid as _uuid
        pid = _uuid.UUID(project_id)
    except Exception:
        return False

    return (
        db.query(UserProjectRole)
        .filter(UserProjectRole.user_id == user.id, UserProjectRole.project_id == pid)
        .first()
        is not None
    )


# ============================================================
#  Active organization context — multi-tenant support
# ============================================================

@dataclass
class ActiveContext:
    """组合：当前登录用户 + 当前所选 organization_id + 可见 orgs + 各 org 下的角色。"""
    user: AppUser
    active_org_id: uuid.UUID
    accessible_org_ids: set[uuid.UUID]
    # 用户在每个可见 org 下的角色。系统管理员为空 dict（视作全权）。
    # active_org_role 是该用户在当前 active_org_id 下的角色；admin 视角下为 None（视为全权）。
    org_roles: dict[uuid.UUID, OrgMemberRole]
    active_org_role: Optional[OrgMemberRole]

    @property
    def is_system_admin(self) -> bool:
        return self.user.is_system_admin

    @property
    def is_org_admin(self) -> bool:
        """是否能在当前激活组织执行管理操作（系统管理员 或 org_admin 角色）。"""
        if self.user.is_system_admin:
            return True
        return self.active_org_role == OrgMemberRole.org_admin


class ActiveOrg:
    """FastAPI dependency，解析当前请求的 active organization。

    返回 dataclass `ActiveContext`，业务端点可用 `ctx.active_org_id` 做过滤；
    也可继续从 `request.state.active_org_id` 读取。

    解析逻辑见 `core.org_context.resolve_active_org`。
    """

    def __init__(self, require_active: bool = True):
        self.require_active = require_active

    async def __call__(
        self,
        request: Request,
        user: AppUser = Depends(CurrentUser()),
        db: Session = Depends(get_db),
    ) -> ActiveContext:
        if self.require_active and user.status != AppUserStatus.active:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="用户已被禁用。")
        active_org_id = resolve_active_org(request, user, db)
        accessible = set(getattr(request.state, "user_org_access", set()) or set())
        org_roles: dict[uuid.UUID, OrgMemberRole] = getattr(request.state, "user_org_roles", {}) or {}
        active_role = org_roles.get(active_org_id)
        return ActiveContext(
            user=user,
            active_org_id=active_org_id,
            accessible_org_ids=accessible,
            org_roles=org_roles,
            active_org_role=active_role,
        )
