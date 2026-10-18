"""Leases CRUD with project-scoped authorization."""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...core.auth import CurrentUser, require_project_scope
from ...core.database import get_db
from ...core.org_context import ensure_project_in_active_org
from ...models import AppUser, AuditLog, Lease, LeaseStatus

router = APIRouter(prefix="/leases", tags=["leases"])


# ---------- Schemas ----------

class LeaseCreate(BaseModel):
    project_id: str
    lease_code: str = Field(min_length=1, max_length=128)
    tenant_id: str
    space_id: str
    contract_signed_date: Optional[str] = None
    start_date: str
    end_date: str
    rent_amount: float = 0
    deposit_amount: float = 0
    payment_cycle: Optional[str] = None
    status: Optional[str] = None


class LeaseUpdate(BaseModel):
    contract_signed_date: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    rent_amount: Optional[float] = None
    deposit_amount: Optional[float] = None
    payment_cycle: Optional[str] = None
    status: Optional[str] = None


# ---------- Helpers ----------

def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def _project_scope_check(project_id: str, user: AppUser, db: Session) -> None:
    if not require_project_scope(project_id, user, db):
        raise HTTPException(status_code=403, detail="无权访问此项目。")


def _serialize(l: Lease) -> dict:
    return {
        "id": str(l.id),
        "project_id": str(l.project_id),
        "lease_code": l.lease_code,
        "tenant_id": str(l.tenant_id),
        "space_id": str(l.space_id),
        "contract_signed_date": l.contract_signed_date.isoformat() if l.contract_signed_date else None,
        "start_date": l.start_date.isoformat(),
        "end_date": l.end_date.isoformat(),
        "rent_amount": float(l.rent_amount),
        "deposit_amount": float(l.deposit_amount),
        "payment_cycle": l.payment_cycle.value if hasattr(l.payment_cycle, "value") else l.payment_cycle,
        "status": l.status.value if hasattr(l.status, "value") else str(l.status),
        "version": l.version,
        "created_at": l.created_at.isoformat(),
        "updated_at": l.updated_at.isoformat(),
    }


# ---------- Endpoints ----------

@router.get("")
def list_leases(
    project_id: str = Query(...),
    status: Optional[str] = None,
    tenant_id: Optional[str] = None,
    space_id: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: AppUser = Depends(CurrentUser()),
):
    _project_scope_check(project_id, user, db)
    pid = uuid.UUID(project_id)
    ensure_project_in_active_org(pid, user, db)
    q = db.query(Lease).filter(Lease.project_id == pid)
    if status:
        q = q.filter(Lease.status == status)
    if tenant_id:
        q = q.filter(Lease.tenant_id == uuid.UUID(tenant_id))
    if space_id:
        q = q.filter(Lease.space_id == uuid.UUID(space_id))

    total = q.count()
    rows = q.order_by(Lease.created_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {"items": [_serialize(x) for x in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/due")
def due_leases(
    project_id: str = Query(...),
    days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
    user: AppUser = Depends(CurrentUser()),
):
    """Contracts expiring within `days` days (excluding terminated/vacated)."""
    _project_scope_check(project_id, user, db)
    pid = uuid.UUID(project_id)
    ensure_project_in_active_org(pid, user, db)
    import datetime as _dt
    today = _dt.date.today()
    cutoff = today + _dt.timedelta(days=days)

    rows = (
        db.query(Lease)
        .filter(
            Lease.project_id == pid,
            Lease.end_date >= today,
            Lease.end_date <= cutoff,
            Lease.status.in_([LeaseStatus.active.value, LeaseStatus.expired.value, LeaseStatus.pending.value]),
        )
        .order_by(Lease.end_date.asc())
        .all()
    )
    return {"items": [_serialize(x) for x in rows], "range": {"from": today.isoformat(), "to": cutoff.isoformat()}}


@router.get("/{lease_id}")
def get_lease(lease_id: str, request: Request = None, db: Session = Depends(get_db), user: AppUser = Depends(CurrentUser())):
    try:
        lid = uuid.UUID(lease_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的合同 ID。")
    l = db.query(Lease).filter(Lease.id == lid).first()
    if not l:
        raise HTTPException(status_code=404, detail="合同不存在。")
    _project_scope_check(str(l.project_id), user, db)
    ensure_project_in_active_org(l.project_id, user, db)
    return _serialize(l)


@router.post("")
def create_lease(payload: LeaseCreate, req: Request, db: Session = Depends(get_db), user: AppUser = Depends(CurrentUser())):
    _project_scope_check(payload.project_id, user, db)
    pid = uuid.UUID(payload.project_id)
    org_id = ensure_project_in_active_org(pid, user, db)

    exists = db.query(Lease).filter(Lease.project_id == pid, Lease.lease_code == payload.lease_code).first()
    if exists:
        raise HTTPException(status_code=409, detail="合同编号已存在。")

    from ...models import Project, Space, Tenant
    proj = db.query(Project).filter(Project.id == pid).first()
    if not proj:
        raise HTTPException(status_code=404, detail="项目不存在。")

    tenant = db.query(Tenant).filter(Tenant.id == uuid.UUID(payload.tenant_id), Tenant.project_id == pid).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="租户不存在或不在该项目内。")
    space = db.query(Space).filter(Space.id == uuid.UUID(payload.space_id), Space.project_id == pid).first()
    if not space:
        raise HTTPException(status_code=404, detail="空间不存在或不在该项目内。")

    new_lease = Lease(
        id=uuid.uuid4(),
        organization_id=org_id,
        project_id=pid,
        lease_code=payload.lease_code,
        tenant_id=tenant.id,
        space_id=space.id,
        contract_signed_date=_parse_date(payload.contract_signed_date) if payload.contract_signed_date else None,
        start_date=_parse_date(payload.start_date),
        end_date=_parse_date(payload.end_date),
        rent_amount=Decimal(str(payload.rent_amount)),
        deposit_amount=Decimal(str(payload.deposit_amount)),
        payment_cycle=payload.payment_cycle,
        status=payload.status or LeaseStatus.pending,
    )
    db.add(new_lease)
    db.flush()
    db.add(
        AuditLog(
            actor_user_id=user.id,
            action="create",
            resource_type="lease",
            resource_id=str(new_lease.id),
            project_id=pid,
            request_id=getattr(req.state, "request_id", None),
        )
    )
    db.commit()
    return _serialize(new_lease)


@router.patch("/{lease_id}")
def update_lease(lease_id: str, payload: LeaseUpdate, req: Request, db: Session = Depends(get_db), user: AppUser = Depends(CurrentUser())):
    try:
        lid = uuid.UUID(lease_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的合同 ID。")

    l = db.query(Lease).filter(Lease.id == lid).first()
    if not l:
        raise HTTPException(status_code=404, detail="合同不存在。")
    _project_scope_check(str(l.project_id), user, db)
    ensure_project_in_active_org(l.project_id, user, db)

    changes = payload.model_dump(exclude_unset=True)
    if changes:
        before = _serialize(l)
        for key, val in changes.items():
            if val is None:
                continue
            if key in ("contract_signed_date", "start_date", "end_date"):
                val = _parse_date(val)
            elif key in ("rent_amount", "deposit_amount"):
                val = Decimal(str(val))
            setattr(l, key, val)
        l.version += 1

        db.add(
            AuditLog(
                actor_user_id=user.id,
                action="update",
                resource_type="lease",
                resource_id=str(l.id),
                project_id=l.project_id,
                before_summary={"fields": changes, "snapshot": before},
                request_id=getattr(req.state, "request_id", None),
            )
        )
        db.commit()
    return _serialize(l)
