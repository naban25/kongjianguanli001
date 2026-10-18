"""Authentication routes: login, logout, me."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...core.auth import (
    CurrentUser,
    create_access_token,
    verify_password,
)
from ...core.config import get_settings
from ...core.database import get_db
from ...models import AppUser, AppUserStatus, AuditLog

router = APIRouter(prefix="/auth", tags=["auth"])

settings = get_settings()


class LoginRequest(BaseModel):
    username: str = Field(min_length=2, max_length=128)
    password: str = Field(min_length=6, max_length=256)


class UserInfo(BaseModel):
    id: str
    username: str
    display_name: str | None
    email: str | None
    is_system_admin: bool
    status: str

    @classmethod
    def from_model(cls, u: AppUser) -> "UserInfo":
        return cls(
            id=str(u.id),
            username=u.username,
            display_name=u.display_name,
            email=u.email,
            is_system_admin=u.is_system_admin,
            status=u.status.value if hasattr(u.status, "value") else str(u.status),
        )


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserInfo


@router.post("/login", response_model=LoginResponse)
def login(req: Request, payload: LoginRequest, response: Response, db: Session = Depends(get_db)):
    """Authenticate a user and return a JWT + set HttpOnly cookie.

    Failure messages are intentionally generic to avoid leaking account existence.
    """
    user = db.query(AppUser).filter(AppUser.username == payload.username).first()
    if not user or not verify_password(payload.password, user.password_hash or ""):
        # Generic message — do not reveal whether account exists
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误。")

    if user.status != AppUserStatus.active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账户已被禁用，请联系管理员。")

    token = create_access_token(user)

    # Update last login
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()

    # Audit
    db.add(
        AuditLog(
            actor_user_id=user.id,
            action="login",
            resource_type="auth",
            request_id=getattr(req.state, "request_id", None),
            ip_address=req.client.host if req.client else None,
        )
    )
    db.commit()

    # Cookie for browser (HttpOnly, configurable SameSite/Secure via env)
    # Cross-origin (Pages ↔ Railway) requires SameSite=None;Secure=True
    response.set_cookie(
        key="session",
        value=token,
        httponly=True,
        samesite=settings.cookie_samesite,
        secure=settings.cookie_secure,
        max_age=settings.jwt_expire_hours * 3600,
        path="/",
    )

    return LoginResponse(
        access_token=token,
        token_type="bearer",
        expires_in=settings.jwt_expire_hours * 3600,
        user=UserInfo.from_model(user),
    )


@router.post("/logout")
def logout(req: Request, response: Response, db: Session = Depends(get_db)):
    response.delete_cookie("session", path="/")

    # Audit logout if user known
    user_id = getattr(getattr(req.state, "current_user", None), "id", None)
    if user_id:
        db.add(
            AuditLog(
                actor_user_id=user_id,
                action="logout",
                resource_type="auth",
                request_id=getattr(req.state, "request_id", None),
            )
        )
        db.commit()

    return {"ok": True}


@router.get("/me", response_model=UserInfo)
def me(user: AppUser = Depends(CurrentUser())):
    return UserInfo.from_model(user)
