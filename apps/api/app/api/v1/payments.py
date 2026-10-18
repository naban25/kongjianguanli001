"""Payments API: 收款记录 CRUD + 汇总。"""
from __future__ import annotations

import uuid
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from ...core.auth import AppUser, CurrentUser, require_project_scope
from ...core.database import get_db
from ...core.org_context import ensure_project_in_active_org
from ...models import (
    Payment,
    PaymentMethod,
    PaymentStatus,
    Tenant,
)

router = APIRouter(prefix="/payments", tags=["payments"])


# 中英枚举映射（与 COLUMN_META 同步）
METHOD_LABELS: dict[str, str] = {
    "bank_transfer": "银行转账",
    "cash": "现金",
    "check": "支票",
    "online": "在线支付",
    "other": "其他",
}
STATUS_LABELS: dict[str, str] = {
    "pending": "处理中",
    "completed": "已完成",
    "refunded": "已退款",
    "cancelled": "已取消",
}


@router.get("")
def list_payments(
    project_id: str = Query(...),
    search: Optional[str] = None,
    status: Optional[str] = None,
    method: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: AppUser = Depends(CurrentUser()),
) -> dict:
    """列出收款记录，支持筛选 + 分页 + 汇总。"""
    if not require_project_scope(project_id, user, db):
        raise HTTPException(status_code=403, detail="无权访问此项目。")
    pid = uuid.UUID(project_id)
    ensure_project_in_active_org(pid, user, db)

    q = db.query(Payment).filter(Payment.project_id == pid)

    if status:
        q = q.filter(Payment.status == PaymentStatus(status))
    if method:
        q = q.filter(Payment.payment_method == PaymentMethod(method))
    if date_from:
        q = q.filter(Payment.payment_date >= date_from)
    if date_to:
        q = q.filter(Payment.payment_date <= date_to)

    if search:
        from sqlalchemy import String as SAString, Enum as SAEnum
        string_cols = [c.name for c in Payment.__table__.columns
                       if isinstance(c.type, SAString) and not isinstance(c.type, SAEnum)]
        if string_cols:
            from sqlalchemy import or_
            q = q.filter(or_(*[getattr(Payment, c).ilike(f"%{search}%") for c in string_cols]))

    total = q.count()

    # 汇总：金额合计 + 按 status 分组
    summary_q = db.query(
        Payment.status,
        sa_func.count(Payment.id).label("count"),
        sa_func.coalesce(sa_func.sum(Payment.amount), 0).label("amount"),
    ).filter(Payment.project_id == pid)
    if date_from:
        summary_q = summary_q.filter(Payment.payment_date >= date_from)
    if date_to:
        summary_q = summary_q.filter(Payment.payment_date <= date_to)
    summary_rows = summary_q.group_by(Payment.status).all()

    total_amount = sum(float(r.amount) for r in summary_rows)
    by_status = {
        (r.status.value if hasattr(r.status, "value") else str(r.status)): {
            "count": r.count,
            "amount": float(r.amount),
        }
        for r in summary_rows
    }

    rows = q.order_by(Payment.payment_date.desc(), Payment.created_at.desc()).offset((page - 1) * page_size).limit(page_size).all()

    # 租户名称 join
    tenant_ids = {p.tenant_id for p in rows if p.tenant_id}
    tenants_map: dict[uuid.UUID, str] = {}
    if tenant_ids:
        for tn in db.query(Tenant).filter(Tenant.id.in_(tenant_ids)).all():
            tenants_map[tn.id] = tn.tenant_name

    items = [
        {
            "id": str(p.id),
            "payment_date": p.payment_date.isoformat() if p.payment_date else None,
            "amount": float(p.amount),
            "currency": p.currency,
            "payment_method": p.payment_method.value if hasattr(p.payment_method, "value") else (p.payment_method or None),
            "payment_method_label": METHOD_LABELS.get(p.payment_method.value if hasattr(p.payment_method, "value") else str(p.payment_method or ""), None),
            "reference_no": p.reference_no,
            "status": p.status.value if hasattr(p.status, "value") else str(p.status),
            "status_label": STATUS_LABELS.get(p.status.value if hasattr(p.status, "value") else str(p.status), None),
            "tenant_id": str(p.tenant_id) if p.tenant_id else None,
            "tenant_name": tenants_map.get(p.tenant_id) if p.tenant_id else None,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }
        for p in rows
    ]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "summary": {
            "total_amount": round(total_amount, 2),
            "by_status": by_status,
        },
    }
