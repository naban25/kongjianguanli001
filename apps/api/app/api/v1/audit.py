"""Audit log queries — admin-only."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ...core.auth import CurrentUser
from ...core.database import get_db
from ...models import AppUser, AppUserStatus, AuditLog

router = APIRouter(prefix="/audit-logs", tags=["audit"])


def _serialize(log: AuditLog) -> dict:
    return {
        "id": str(log.id),
        "actor_user_id": str(log.actor_user_id) if log.actor_user_id else None,
        "action": log.action,
        "resource_type": log.resource_type,
        "resource_id": log.resource_id,
        "project_id": str(log.project_id) if log.project_id else None,
        "ip_address": log.ip_address,
        "request_id": log.request_id,
        "before_summary": log.before_summary,
        "after_summary": log.after_summary,
        "created_at": log.created_at.isoformat(),
    }


@router.get("")
def list_logs(
    project_id: Optional[str] = None,
    action: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: AppUser = Depends(CurrentUser()),
):
    """Audit logs. System admin sees everything; project admins see their own project; operators see nothing."""
    if not user.is_system_admin:
        # Project admins see project-scoped logs only
        if not project_id:
            raise HTTPException(status_code=400, detail="项目管理员必须指定 project_id。")
        try:
            import uuid as _uuid
            pid = _uuid.UUID(project_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="无效的项目 ID。")

        from ...models import UserProjectRole
        is_pa = (
            db.query(UserProjectRole)
            .filter(UserProjectRole.user_id == user.id, UserProjectRole.project_id == pid)
            .first()
        )
        if not is_pa:
            raise HTTPException(status_code=403, detail="无权查看审计日志。")

    q = db.query(AuditLog)
    if not user.is_system_admin:
        q = q.filter(AuditLog.project_id == pid)
    elif project_id:
        try:
            q = q.filter(AuditLog.project_id == uuid.UUID(project_id))
        except ValueError:
            pass
    if action:
        q = q.filter(AuditLog.action == action)

    total = q.count()
    rows = q.order_by(AuditLog.created_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {"items": [_serialize(r) for r in rows], "total": total, "page": page, "page_size": page_size}
