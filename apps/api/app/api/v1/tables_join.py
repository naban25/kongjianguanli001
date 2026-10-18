"""跨表 join 视图：根据预定义视图配置，联表查询租户/租约/费用/收款/空间。

支持的视图（V1）：
- tenant_overview: 租户 + 该租户的租约数 + 该租户的应收/已收汇总
- lease_detail:   租约 + 租户 + 空间 + 该租约的费用合计 / 收款合计
- space_tenants:  空间 + 当前活跃租约数 + 当前活跃租户
- overdue_view:   逾期费用 + 租户 + 租约 + 空间 一体化视图
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func as sa_func
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
    PaymentAllocation,
    PaymentStatus,
    Space,
    Tenant,
)

router = APIRouter(prefix="/tables/join", tags=["tables-join"])


def _to_dict(model, fields: list[str]) -> dict:
    """将 ORM 实例转为按 fields 提取的字典。"""
    out: dict[str, Any] = {}
    for f in fields:
        v = getattr(model, f, None)
        if hasattr(v, "isoformat"):
            out[f] = v.isoformat()
        elif hasattr(v, "value"):
            out[f] = v.value
        elif isinstance(v, uuid.UUID):
            out[f] = str(v)
        else:
            out[f] = v
    return out


@router.get("/tenant_overview")
def tenant_overview(
    project_id: str,
    search: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: AppUser = Depends(CurrentUser()),
) -> dict:
    """租户总览：每个租户 + 关联统计（租约数 / 应收 / 已收）。"""
    if not require_project_scope(project_id, user, db):
        raise HTTPException(status_code=403, detail="无权访问此项目。")
    pid = uuid.UUID(project_id)
    ensure_project_in_active_org(pid, user, db)

    tenants_q = db.query(Tenant).filter(Tenant.project_id == pid)
    if search:
        tenants_q = tenants_q.filter(Tenant.tenant_name.ilike(f"%{search}%"))

    total = tenants_q.count()
    tenants = tenants_q.order_by(Tenant.tenant_name.asc()).offset((page - 1) * page_size).limit(page_size).all()
    tenant_ids = [t.id for t in tenants]
    if not tenant_ids:
        return {"items": [], "total": total, "page": page, "page_size": page_size}

    # 关联统计：每个租户的租约数、应收总额、已分配收款总额
    lease_counts = dict(
        db.query(Lease.tenant_id, sa_func.count(Lease.id))
        .filter(Lease.tenant_id.in_(tenant_ids), Lease.status != LeaseStatus.terminated.value)
        .group_by(Lease.tenant_id).all()
    )
    charge_totals = dict(
        db.query(Charge.tenant_id, sa_func.coalesce(sa_func.sum(Charge.amount_due), 0))
        .filter(Charge.tenant_id.in_(tenant_ids))
        .group_by(Charge.tenant_id).all()
    )
    paid_totals = dict(
        db.query(Payment.tenant_id, sa_func.coalesce(sa_func.sum(Payment.amount), 0))
        .filter(Payment.tenant_id.in_(tenant_ids), Payment.status == PaymentStatus.completed.value)
        .group_by(Payment.tenant_id).all()
    )

    items = []
    for t in tenants:
        # 注：联系人是加密存储（contact_phone_encrypted），不在跨表 JOIN 视图返回
        # 如需查询，请使用 assistant 工具带脱敏处理
        items.append({
            "租户名称": t.tenant_name,
            "租户类型": t.tenant_type.value if hasattr(t.tenant_type, "value") else t.tenant_type,
            "行业": t.industry,
            "联系人": t.contact_name,
            "联系电话": None,
            "统一社会信用代码": t.registration_code,
            "租约数": lease_counts.get(t.id, 0),
            "应收总额": round(float(charge_totals.get(t.id, 0) or 0), 2),
            "已收总额": round(float(paid_totals.get(t.id, 0) or 0), 2),
            "未收余额": round(
                float(charge_totals.get(t.id, 0) or 0) - float(paid_totals.get(t.id, 0) or 0), 2),
        })
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/lease_detail")
def lease_detail(
    project_id: str,
    search: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: AppUser = Depends(CurrentUser()),
) -> dict:
    """租约明细：租约 + 租户 + 空间 + 费用合计 + 收款合计 + 余额。"""
    if not require_project_scope(project_id, user, db):
        raise HTTPException(status_code=403, detail="无权访问此项目。")
    pid = uuid.UUID(project_id)
    ensure_project_in_active_org(pid, user, db)

    q = (
        db.query(Lease, Tenant, Space)
        .outerjoin(Tenant, Tenant.id == Lease.tenant_id)
        .outerjoin(Space, Space.id == Lease.space_id)
        .filter(Lease.project_id == pid)
    )
    if status:
        q = q.filter(Lease.status == LeaseStatus(status))
    if search:
        q = q.filter(
            (Lease.lease_code.ilike(f"%{search}%"))
            | (Tenant.tenant_name.ilike(f"%{search}%"))
            | (Space.space_code.ilike(f"%{search}%"))
        )

    total = q.count()
    rows = q.order_by(Lease.start_date.desc()).offset((page - 1) * page_size).limit(page_size).all()

    lease_ids = [l.id for l, _, _ in rows]

    charge_by_lease = dict(
        db.query(Charge.lease_id, sa_func.coalesce(sa_func.sum(Charge.amount_due), 0))
        .filter(Charge.lease_id.in_(lease_ids))
        .group_by(Charge.lease_id).all()
    )
    paid_by_lease = dict(
        db.query(Charge.lease_id, sa_func.coalesce(sa_func.sum(PaymentAllocation.allocated_amount), 0))
        .join(Payment, Payment.id == PaymentAllocation.payment_id)
        .filter(
            Charge.lease_id.in_(lease_ids),
            Payment.status == PaymentStatus.completed.value,
        )
        .group_by(Charge.lease_id).all()
    )

    items = []
    for lease, tenant, space in rows:
        total_due = float(charge_by_lease.get(lease.id, 0) or 0)
        total_paid = float(paid_by_lease.get(lease.id, 0) or 0)
        items.append({
            "租约编号": lease.lease_code,
            "开始日期": lease.start_date.isoformat() if lease.start_date else None,
            "结束日期": lease.end_date.isoformat() if lease.end_date else None,
            "状态": lease.status.value if hasattr(lease.status, "value") else lease.status,
            "租金": float(lease.rent_amount) if lease.rent_amount else 0.0,
            "押金": float(lease.deposit_amount) if lease.deposit_amount else 0.0,
            "付款周期": lease.payment_cycle.value if hasattr(lease.payment_cycle, "value") else lease.payment_cycle,
            "租户": tenant.tenant_name if tenant else None,
            "空间编号": space.space_code if space else None,
            "楼层": space.floor if space else None,
            "面积（㎡）": float(space.area_sqm) if space and space.area_sqm else None,
            "应收合计": round(total_due, 2),
            "已收合计": round(total_paid, 2),
            "未收余额": round(total_due - total_paid, 2),
        })
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/space_tenants")
def space_tenants(
    project_id: str,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    user: AppUser = Depends(CurrentUser()),
) -> dict:
    """空间 × 当前租户：空间 + 活跃租约 + 租户名称。"""
    if not require_project_scope(project_id, user, db):
        raise HTTPException(status_code=403, detail="无权访问此项目。")
    pid = uuid.UUID(project_id)
    ensure_project_in_active_org(pid, user, db)

    q = db.query(Space).filter(Space.project_id == pid)
    if status:
        q = q.filter(Space.status == status)
    spaces = q.order_by(Space.space_code.asc()).all()

    space_ids = [s.id for s in spaces]
    today = date.today()
    active_leases = {}
    if space_ids:
        leases = (
            db.query(Lease)
            .filter(
                Lease.space_id.in_(space_ids),
                Lease.status == LeaseStatus.active.value,
                Lease.start_date <= today,
                Lease.end_date >= today,
            )
            .all()
        )
        tenant_ids = {l.tenant_id for l in leases}
        tenants_map = {}
        if tenant_ids:
            for tn in db.query(Tenant).filter(Tenant.id.in_(tenant_ids)).all():
                tenants_map[tn.id] = tn.tenant_name

        for l in leases:
            existing = active_leases.get(l.space_id)
            if existing is None or l.end_date > existing["end_date"]:
                active_leases[l.space_id] = {
                    "租约编号": l.lease_code,
                    "租户": tenants_map.get(l.tenant_id),
                    "租约到期": l.end_date.isoformat() if l.end_date else None,
                }

    items = []
    for s in spaces:
        active = active_leases.get(s.id)
        items.append({
            "空间编号": s.space_code,
            "楼层": s.floor,
            "面积（㎡）": float(s.area_sqm) if s.area_sqm else None,
            "工位数": s.workstation_count,
            "状态": s.status.value if hasattr(s.status, "value") else s.status,
            "当前租约": active,
        })
    return {"items": items, "total": len(items)}


@router.get("/overdue_view")
def overdue_view(
    project_id: str,
    min_days: int = Query(0, ge=0, le=365),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: AppUser = Depends(CurrentUser()),
) -> dict:
    """逾期视图：逾期费用 + 租户 + 租约 + 空间（一体化）。"""
    if not require_project_scope(project_id, user, db):
        raise HTTPException(status_code=403, detail="无权访问此项目。")
    pid = uuid.UUID(project_id)
    ensure_project_in_active_org(pid, user, db)
    today = date.today()
    cutoff = today - timedelta(days=min_days)

    rows = (
        db.query(Charge, Tenant, Lease, Space)
        .outerjoin(Tenant, Tenant.id == Charge.tenant_id)
        .outerjoin(Lease, Lease.id == Charge.lease_id)
        .outerjoin(Space, Space.id == Lease.space_id)
        .filter(
            Charge.project_id == pid,
            Charge.due_date < today,
            Charge.due_date <= cutoff,
            Charge.status.notin_([ChargeStatus.paid.value, ChargeStatus.cancelled.value]),
        )
        .order_by(Charge.due_date.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    total = (
        db.query(sa_func.count(Charge.id))
        .filter(
            Charge.project_id == pid,
            Charge.due_date < today,
            Charge.status.notin_([ChargeStatus.paid.value, ChargeStatus.cancelled.value]),
        )
        .scalar()
    )
    total_amount = (
        db.query(sa_func.coalesce(sa_func.sum(Charge.amount_due), 0))
        .filter(
            Charge.project_id == pid,
            Charge.due_date < today,
            Charge.status.notin_([ChargeStatus.paid.value, ChargeStatus.cancelled.value]),
        )
        .scalar()
    )

    items = []
    for c, t, l, s in rows:
        items.append({
            "费用类型": c.charge_type.value if hasattr(c.charge_type, "value") else c.charge_type,
            "金额": float(c.amount_due) if c.amount_due else 0.0,
            "应付款日": c.due_date.isoformat() if c.due_date else None,
            "逾期天数": (today - c.due_date).days if c.due_date else None,
            "租户": t.tenant_name if t else None,
            "联系电话": None,
            "租约编号": l.lease_code if l else None,
            "空间编号": s.space_code if s else None,
        })
    return {
        "items": items,
        "total": total or 0,
        "page": page,
        "page_size": page_size,
        "summary": {
            "total_amount": round(float(total_amount or 0), 2),
            "as_of": today.isoformat(),
        },
    }
