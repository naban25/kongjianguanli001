"""Dashboard endpoints: reminders (due leases / overdue) + monthly financials."""
from __future__ import annotations

import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ...core.auth import AppUser, CurrentUser, require_project_scope
from ...core.database import get_db
from ...core.org_context import ensure_project_in_active_org
from ...models import (
    Charge,
    ChargeStatus,
    Lease,
    LeaseStatus,
    Payment,
    PaymentStatus,
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


# ============================================================
# 本月财务汇总：本月应收 / 已收 / 未收 + 详细账目
# ============================================================


def _resolve_month(year_month: str | None) -> tuple[date, date, str]:
    """解析 'YYYY-MM'，返回本月起止 [start, end) 与 'YYYY-MM' 字符串。

    end_exclusive 是下个月的第一天（end 区间为半开区间 [start, end_exclusive)）。
    """
    if year_month:
        try:
            parts = year_month.split("-")
            y, m = int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            raise HTTPException(status_code=400, detail="year_month 格式应为 YYYY-MM。")
        if not (2000 <= y <= 2100 and 1 <= m <= 12):
            raise HTTPException(status_code=400, detail="year_month 范围无效。")
    else:
        today = date.today()
        y, m = today.year, today.month
        year_month = f"{y:04d}-{m:02d}"
    start = date(y, m, 1)
    # 下个月第一天：跨年时 +1 年
    if m == 12:
        end_exclusive = date(y + 1, 1, 1)
    else:
        end_exclusive = date(y, m + 1, 1)
    return start, end_exclusive, year_month


@router.get("/financials")
def dashboard_financials(
    project_id: str = Query(...),
    year_month: str | None = Query(None, description="YYYY-MM；缺省为当前月"),
    db: Session = Depends(get_db),
    user: AppUser = Depends(CurrentUser()),
) -> dict:
    """本月财务汇总：本月应收 / 已收 / 未收 + 详细账目（用于工作台点击展开）。

    本月应收 = charges.amount_due（按 due_date 落在本月，未取消）
    本月已收 = payments.amount（按 payment_date 落在本月，状态 = 已完成）
    本月未收 = 应收 - 已收（最低 0）

    详细账目：
    - charges：本月所有未取消的费用（应收）
    - payments：本月所有已完成的收款（已收）
    - items 同时返回用于在「多维表格」按预筛选跳转的 deep-link
    """
    if not require_project_scope(project_id, user, db):
        raise HTTPException(status_code=403, detail="无权访问此项目。")
    pid = uuid.UUID(project_id)
    ensure_project_in_active_org(pid, user, db)
    start, end_exclusive, ym = _resolve_month(year_month)

    # 1. 本月应收：due_date ∈ [start, end_exclusive) 且 status != cancelled
    charges_q = (
        db.query(Charge)
        .filter(
            Charge.project_id == pid,
            Charge.due_date >= start,
            Charge.due_date < end_exclusive,
            Charge.status != ChargeStatus.cancelled.value,
        )
        .order_by(Charge.due_date.asc())
    )
    charge_rows = charges_q.all()
    charges_payload = [
        {
            "charge_id": str(c.id),
            "tenant_id": str(c.tenant_id),
            "charge_type": c.charge_type.value if hasattr(c.charge_type, "value") else str(c.charge_type),
            "amount_due": float(c.amount_due) if c.amount_due else 0.0,
            "amount_paid": float(getattr(c, "amount_paid", 0) or 0),
            "due_date": c.due_date.isoformat() if c.due_date else None,
            "period_start": c.period_start.isoformat() if getattr(c, "period_start", None) else None,
            "period_end": c.period_end.isoformat() if getattr(c, "period_end", None) else None,
            "status": c.status.value if hasattr(c.status, "value") else str(c.status),
        }
        for c in charge_rows
    ]
    total_due = round(sum(c["amount_due"] for c in charges_payload), 2)
    total_charge_paid = round(sum(c["amount_paid"] for c in charges_payload), 2)

    # 2. 本月已收：payment_date ∈ [start, end_exclusive) 且 status = completed
    payments_q = (
        db.query(Payment)
        .filter(
            Payment.project_id == pid,
            Payment.payment_date >= start,
            Payment.payment_date < end_exclusive,
            Payment.status == PaymentStatus.completed.value,
        )
        .order_by(Payment.payment_date.asc())
    )
    payment_rows = payments_q.all()
    payments_payload = [
        {
            "payment_id": str(p.id),
            "tenant_id": str(p.tenant_id),
            "amount": float(p.amount) if p.amount else 0.0,
            "payment_method": p.payment_method.value if hasattr(p.payment_method, "value") else str(p.payment_method),
            "payment_date": p.payment_date.isoformat() if p.payment_date else None,
            "reference_no": p.reference_no,
            "status": p.status.value if hasattr(p.status, "value") else str(p.status),
        }
        for p in payment_rows
    ]
    total_paid = round(sum(p["amount"] for p in payments_payload), 2)

    # 3. 本月未收 = 应收 - 已收（按费用侧视角，未收到的部分）
    total_outstanding = round(max(total_due - total_charge_paid, 0.0), 2)

    return {
        "year_month": ym,
        "period_start": start.isoformat(),
        "period_end_exclusive": end_exclusive.isoformat(),
        "totals": {
            "due": total_due,
            "paid": total_paid,
            "outstanding": total_outstanding,
            "charge_count": len(charges_payload),
            "payment_count": len(payments_payload),
        },
        "charges": charges_payload,
        "payments": payments_payload,
    }
