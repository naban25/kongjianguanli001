"""Dashboard reminders: due leases + overdue charges + upcoming charges."""
from __future__ import annotations

import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ...core.auth import AppUser, CurrentUser, require_project_scope
from ...core.database import get_db
from ...core.org_context import ensure_project_in_active_org
from ...models import (
    Charge,
    ChargeStatus,
    Lease,
    LeaseStatus,
    Tenant,
)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/reminders")
def dashboard_reminders(
    project_id: str = Query(...),
    due_days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
    user: AppUser = Depends(CurrentUser()),
) -> dict:
    """聚合 dashboard 提醒：到期合同 + 逾期费用 + 即将到期费用（7 天内）。"""
    if not require_project_scope(project_id, user, db):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="无权访问此项目。")

    pid = uuid.UUID(project_id)
    ensure_project_in_active_org(pid, user, db)
    return _build_reminders(db, pid, due_days)


def _build_reminders(db: Session, project_id: uuid.UUID, due_days: int) -> dict:
    """聚合 dashboard 提醒：到期合同 + 逾期费用 + 即将到期费用。"""
    today = date.today()
    due_cutoff = today + timedelta(days=due_days)

    # 1. 到期合同（active/pending，end_date 在 [today, due_cutoff]）
    due_lease_rows = (
        db.query(Lease)
        .filter(
            Lease.project_id == project_id,
            Lease.end_date >= today,
            Lease.end_date <= due_cutoff,
            Lease.status.in_([
                LeaseStatus.active.value,
                LeaseStatus.pending.value,
            ]),
        )
        .order_by(Lease.end_date.asc())
        .limit(50)
        .all()
    )
    tenant_ids = {l.tenant_id for l in due_lease_rows}
    tenants_map: dict[uuid.UUID, str] = {}
    if tenant_ids:
        for tn in db.query(Tenant).filter(Tenant.id.in_(tenant_ids)).all():
            tenants_map[tn.id] = tn.tenant_name

    due_leases = [
        {
            "lease_id": str(l.id),
            "lease_code": l.lease_code,
            "tenant": tenants_map.get(l.tenant_id),
            "end_date": l.end_date.isoformat() if l.end_date else None,
            "days_left": (l.end_date - today).days if l.end_date else None,
            "status": l.status.value if hasattr(l.status, "value") else str(l.status),
        }
        for l in due_lease_rows
    ]

    # 2. 逾期费用（due_date < today，未 paid/cancelled）
    overdue_rows = (
        db.query(Charge)
        .filter(
            Charge.project_id == project_id,
            Charge.due_date < today,
            Charge.status.notin_([ChargeStatus.paid.value, ChargeStatus.cancelled.value]),
        )
        .order_by(Charge.due_date.asc())
        .limit(50)
        .all()
    )
    overdue_charges = [
        {
            "charge_id": str(c.id),
            "tenant_id": str(c.tenant_id),
            "charge_type": c.charge_type.value if hasattr(c.charge_type, "value") else str(c.charge_type),
            "amount_due": float(c.amount_due) if c.amount_due else 0.0,
            "due_date": c.due_date.isoformat() if c.due_date else None,
            "days_overdue": (today - c.due_date).days if c.due_date else None,
            "status": c.status.value if hasattr(c.status, "value") else str(c.status),
        }
        for c in overdue_rows
    ]
    overdue_amount_total = sum(c["amount_due"] for c in overdue_charges)

    # 3. 即将到期费用（7 天内到期，未 paid/cancelled）
    week_cutoff = today + timedelta(days=7)
    upcoming_rows = (
        db.query(Charge)
        .filter(
            Charge.project_id == project_id,
            Charge.due_date >= today,
            Charge.due_date <= week_cutoff,
            Charge.status.notin_([ChargeStatus.paid.value, ChargeStatus.cancelled.value]),
        )
        .order_by(Charge.due_date.asc())
        .limit(20)
        .all()
    )
    upcoming_charges = [
        {
            "charge_id": str(c.id),
            "charge_type": c.charge_type.value if hasattr(c.charge_type, "value") else str(c.charge_type),
            "amount_due": float(c.amount_due) if c.amount_due else 0.0,
            "due_date": c.due_date.isoformat() if c.due_date else None,
        }
        for c in upcoming_rows
    ]

    return {
        "as_of": today.isoformat(),
        "due_leases": {
            "count": len(due_leases),
            "items": due_leases,
            "window_days": due_days,
        },
        "overdue_charges": {
            "count": len(overdue_charges),
            "amount_total": round(overdue_amount_total, 2),
            "items": overdue_charges,
        },
        "upcoming_charges": {
            "count": len(upcoming_charges),
            "items": upcoming_charges,
        },
    }
