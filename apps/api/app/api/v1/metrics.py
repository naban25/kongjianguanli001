"""Metrics: occupancy, receivables, tenant counts."""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from ...core.auth import CurrentUser, require_project_scope
from ...core.database import get_db
from ...core.org_context import ensure_project_in_active_org
from ...models import AppUser, Charge, ChargeStatus, Lease, LeaseStatus, Space, SpaceStatus

router = APIRouter(prefix="/metrics", tags=["metrics"])


def _space_base_filters(pid: uuid.UUID):
    return [Space.project_id == pid, Space.status != SpaceStatus.inactive]


@router.get("/occupancy")
def occupancy_metrics(
    project_id: str = Query(...),
    db: Session = Depends(get_db),
    user: AppUser = Depends(CurrentUser()),
) -> dict[str, Any]:
    """出租率：空间数量、面积、工位三个口径。分母为 0 时返回 None，不返回 NaN。"""
    if not require_project_scope(project_id, user, db):
        raise HTTPException(status_code=403, detail="无权访问此项目。")
    pid = uuid.UUID(project_id)
    ensure_project_in_active_org(pid, user, db)

    base_cond = _space_base_filters(pid)

    # 总量
    total_spaces = db.query(Space).filter(*base_cond).count()
    total_area = db.query(func.coalesce(func.sum(Space.area_sqm), Decimal("0"))).filter(*base_cond).scalar() or Decimal("0")
    total_workstations = (
        db.query(func.coalesce(func.sum(Space.workstation_count), 0)).filter(*base_cond).scalar() or 0
    )

    # 已租 (status == leased)
    leased_cond = base_cond + [Space.status == SpaceStatus.leased]
    leased_count = db.query(Space).filter(*leased_cond).count()
    leased_area = db.query(func.coalesce(func.sum(Space.area_sqm), Decimal("0"))).filter(*leased_cond).scalar() or Decimal("0")
    leased_workstations = (
        db.query(func.coalesce(func.sum(Space.workstation_count), 0)).filter(*leased_cond).scalar() or 0
    )

    active_lease_count = (
        db.query(func.count(Lease.id))
        .filter(
            Lease.project_id == pid,
            Lease.status.in_([LeaseStatus.active.value, LeaseStatus.pending.value]),
        )
        .scalar()
        or 0
    )

    def _pct(a: float | int, b: float | int) -> float | None:
        return round(float(a) / float(b) * 100, 2) if b > 0 else None

    return {
        "project_id": project_id,
        "space_count_occupancy": {
            "numerator": leased_count,
            "denominator": total_spaces,
            "ratio_pct": _pct(leased_count, total_spaces),
            "note": "已租空间数 / 可出租空间总数（排除停用）",
        },
        "area_occupancy": {
            "numerator": float(leased_area),
            "denominator": float(total_area),
            "ratio_pct": _pct(float(leased_area), float(total_area)),
            "note": "已租面积 / 可出租总面积",
        },
        "workstation_occupancy": {
            "numerator": leased_workstations,
            "denominator": total_workstations,
            "ratio_pct": _pct(leased_workstations, total_workstations),
            "note": "已租工位 / 可出租工位",
        },
        "active_lease_count": active_lease_count,
        "as_of": date.today().isoformat(),
    }


@router.get("/receivables")
def receivables_metrics(
    project_id: str = Query(...),
    db: Session = Depends(get_db),
    user: AppUser = Depends(CurrentUser()),
) -> dict[str, Any]:
    """应收 / 未收 / 逾期 汇总。"""
    if not require_project_scope(project_id, user, db):
        raise HTTPException(status_code=403, detail="无权访问此项目。")
    pid = uuid.UUID(project_id)
    ensure_project_in_active_org(pid, user, db)

    total_due = (
        db.query(func.coalesce(func.sum(Charge.amount_due), Decimal("0")))
        .filter(Charge.project_id == pid)
        .scalar()
        or Decimal("0")
    )
    by_status = (
        db.query(Charge.status, func.coalesce(func.sum(Charge.amount_due), Decimal("0")))
        .filter(Charge.project_id == pid)
        .group_by(Charge.status)
        .all()
    )
    today = date.today()
    overdue_sum = (
        db.query(func.coalesce(func.sum(Charge.amount_due), Decimal("0")))
        .filter(
            Charge.project_id == pid,
            Charge.due_date < today,
            Charge.status.notin_([ChargeStatus.paid.value, ChargeStatus.cancelled.value]),
        )
        .scalar()
        or Decimal("0")
    )

    return {
        "project_id": project_id,
        "total_amount_due": float(total_due),
        "by_status": {str(s): float(v) for s, v in by_status},
        "overdue_amount": float(overdue_sum),
        "as_of": today.isoformat(),
        "notes": {
            "unpaid_definition": "未收 = 应收金额 - 已核销有效收款金额",
            "overdue_definition": "due_date < 今天 且 status 非 paid/cancelled",
        },
    }
