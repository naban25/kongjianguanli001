"""Spaces CRUD with project-scoped authorization."""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...core.auth import CurrentUser, require_project_scope
from ...core.database import get_db
from ...core.org_context import ensure_project_in_active_org
from ...models import AppUser, AuditLog, Project, Space, SpaceStatus

router = APIRouter(prefix="/spaces", tags=["spaces"])


# ---------- Schemas ----------

class SpaceCreate(BaseModel):
    project_id: str
    building_id: Optional[str] = None
    space_code: str = Field(min_length=1, max_length=128)
    original_space_code: Optional[str] = None
    floor: Optional[int] = None
    space_type: Optional[str] = None
    area_sqm: Optional[float] = None
    workstation_count: Optional[int] = None
    status: Optional[str] = None


class SpaceUpdate(BaseModel):
    building_id: Optional[str] = None
    space_code: Optional[str] = None
    original_space_code: Optional[str] = None
    floor: Optional[int] = None
    space_type: Optional[str] = None
    area_sqm: Optional[float] = None
    workstation_count: Optional[int] = None
    status: Optional[str] = None


# ---------- Helpers ----------

def _project_scope_check(project_id: str, user: AppUser, db: Session) -> None:
    if not require_project_scope(project_id, user, db):
        raise HTTPException(status_code=403, detail="无权访问此项目。")


def _serialize(s: Space) -> dict:
    return {
        "id": str(s.id),
        "project_id": str(s.project_id),
        "building_id": str(s.building_id) if s.building_id else None,
        "space_code": s.space_code,
        "original_space_code": s.original_space_code,
        "floor": s.floor,
        "space_type": s.space_type.value if hasattr(s.space_type, "value") else s.space_type,
        "area_sqm": float(s.area_sqm) if s.area_sqm is not None else None,
        "workstation_count": s.workstation_count,
        "status": s.status.value if hasattr(s.status, "value") else str(s.status),
        "version": s.version,
        "created_at": s.created_at.isoformat(),
        "updated_at": s.updated_at.isoformat(),
    }


# ---------- Endpoints ----------

@router.get("")
def list_spaces(
    project_id: str = Query(..., description="项目 ID（作为查询参数）"),
    status: Optional[str] = None,
    search: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: AppUser = Depends(CurrentUser()),
):
    _project_scope_check(project_id, user, db)
    pid = uuid.UUID(project_id)
    ensure_project_in_active_org(pid, user, db)

    q = db.query(Space).filter(Space.project_id == pid)
    if status:
        q = q.filter(Space.status == status)
    if search:
        q = q.filter(Space.space_code.ilike(f"%{search}%"))

    total = q.count()
    rows = q.order_by(Space.created_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {"items": [_serialize(s) for s in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/{space_id}")
def get_space(space_id: str, request: Request = None, db: Session = Depends(get_db), user: AppUser = Depends(CurrentUser())):
    try:
        sid = uuid.UUID(space_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的空间 ID。")

    space = db.query(Space).filter(Space.id == sid).first()
    if not space:
        raise HTTPException(status_code=404, detail="空间不存在。")
    _project_scope_check(str(space.project_id), user, db)
    ensure_project_in_active_org(space.project_id, user, db)
    return _serialize(space)


@router.post("")
def create_space(payload: SpaceCreate, req: Request, db: Session = Depends(get_db), user: AppUser = Depends(CurrentUser())):
    _project_scope_check(payload.project_id, user, db)
    pid = uuid.UUID(payload.project_id)
    org_id = ensure_project_in_active_org(pid, user, db)

    # Deduplicate code within project
    exists = db.query(Space).filter(Space.project_id == pid, Space.space_code == payload.space_code).first()
    if exists:
        raise HTTPException(status_code=409, detail="该空间编号在项目中已存在。")

    org = db.query(Project).filter(Project.id == pid).first()
    if not org:
        raise HTTPException(status_code=404, detail="项目不存在。")

    from decimal import Decimal
    new_space = Space(
        id=uuid.uuid4(),
        organization_id=org_id,
        project_id=pid,
        building_id=uuid.UUID(payload.building_id) if payload.building_id else None,
        space_code=payload.space_code,
        original_space_code=payload.original_space_code or payload.space_code,
        floor=payload.floor,
        space_type=payload.space_type,
        area_sqm=Decimal(str(payload.area_sqm)) if payload.area_sqm is not None else None,
        workstation_count=payload.workstation_count,
        status=payload.status or SpaceStatus.unknown,
    )
    db.add(new_space)
    db.flush()

    db.add(
        AuditLog(
            actor_user_id=user.id,
            action="create",
            resource_type="space",
            resource_id=str(new_space.id),
            project_id=pid,
            request_id=getattr(req.state, "request_id", None),
        )
    )
    db.commit()
    return _serialize(new_space)


@router.patch("/{space_id}")
def update_space(
    space_id: str,
    payload: SpaceUpdate,
    req: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(CurrentUser()),
):
    try:
        sid = uuid.UUID(space_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的空间 ID。")

    space = db.query(Space).filter(Space.id == sid).first()
    if not space:
        raise HTTPException(status_code=404, detail="空间不存在。")
    _project_scope_check(str(space.project_id), user, db)
    ensure_project_in_active_org(space.project_id, user, db)

    # Version check to avoid silent overwrite
    incoming_version = space.version
    changes = payload.model_dump(exclude_unset=True)
    if changes:
        from decimal import Decimal
        before = _serialize(space)
        for key, val in changes.items():
            if key == "area_sqm" and val is not None:
                val = Decimal(str(val))
            setattr(space, key, val)
        space.version = incoming_version + 1

        db.add(
            AuditLog(
                actor_user_id=user.id,
                action="update",
                resource_type="space",
                resource_id=str(space.id),
                project_id=space.project_id,
                before_summary={"id": str(space.id), "fields": changes, "snapshot": before},
                request_id=getattr(req.state, "request_id", None),
            )
        )
        db.commit()

    return _serialize(space)
