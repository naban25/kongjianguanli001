"""AI assistant: conversation management + agent tool router."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from ...core.auth import CurrentUser, require_project_scope
from ...core.config import get_settings
from ...core.database import get_db
from ...models import (
    AIConversation,
    AIMessage,
    AIToolRun,
    AppUser,
    AuditLog,
    Charge,
    ChargeStatus,
    Lease,
    LeaseStatus,
    Payment,
    PaymentAllocation,
    Space,
    Tenant,
)

settings = get_settings()

router = APIRouter(prefix="/assistant", tags=["assistant"])

# Lazy import so we don't fail at module load if httpx isn't installed
_llm_ok = False
try:
    from ...services import llm_provider
    _llm_ok = True
except Exception:
    llm_provider = None  # type: ignore[assignment]


# ---------- Schemas ----------

class NewConversation(BaseModel):
    project_id: str
    title: Optional[str] = None


class SendMessage(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


# ---------- Tool implementations (read-only, rule-based) ----------

def _tool_get_available_tables(db: Session, user: AppUser, project_id: uuid.UUID) -> dict:
    # Reuse tables router logic
    from .tables import _business_records
    table_names = ["spaces", "tenants", "leases", "charges", "payments"]
    counts = {}
    for t in table_names:
        rows = _business_records(db, project_id, t, 1, 1, None, None, "asc")
        counts[t] = rows["total"]
    return {"tables": [{"name": t, "rows": counts[t]} for t in table_names if counts[t] > 0]}


def _tool_get_business_metrics(db: Session, user: AppUser, project_id: uuid.UUID) -> dict:
    from .metrics import occupancy_metrics, receivables_metrics
    try:
        occ = occupancy_metrics(project_id=str(project_id), db=db, user=user)
        recv = receivables_metrics(project_id=str(project_id), db=db, user=user)
        return {"occupancy": occ, "receivables": recv}
    except Exception as exc:
        return {"error": str(exc)}


def _tool_search_spaces(db: Session, user: AppUser, project_id: uuid.UUID, filters: dict) -> dict:
    q = db.query(Space).filter(Space.project_id == project_id)
    status = filters.get("status")
    if status:
        q = q.filter(Space.status == status)
    search = filters.get("search")
    if search:
        q = q.filter(Space.space_code.ilike(f"%{search}%"))
    rows = q.limit(100).all()
    items = []
    for s in rows:
        items.append({
            "id": str(s.id),
            "space_code": s.space_code,
            "floor": s.floor,
            "area_sqm": float(s.area_sqm) if s.area_sqm else None,
            "status": s.status.value if hasattr(s.status, "value") else str(s.status),
        })
    return {"row_count": len(items), "items": items}


def _tool_due_leases(db: Session, user: AppUser, project_id: uuid.UUID, days: int = 30) -> dict:
    from datetime import date, timedelta
    today = date.today()
    cutoff = today + timedelta(days=days)
    rows = (
        db.query(Lease)
        .filter(
            Lease.project_id == project_id,
            Lease.end_date >= today,
            Lease.end_date <= cutoff,
            Lease.status.in_([LeaseStatus.active.value, LeaseStatus.expired.value, LeaseStatus.pending.value]),
        )
        .order_by(Lease.end_date.asc())
        .limit(100)
        .all()
    )
    items = []
    for l in rows:
        tn = db.query(Tenant).filter(Tenant.id == l.tenant_id).first()
        items.append({
            "lease_code": l.lease_code,
            "tenant": tn.tenant_name if tn else None,
            "end_date": l.end_date.isoformat(),
            "status": l.status.value if hasattr(l.status, "value") else str(l.status),
        })
    return {"row_count": len(items), "items": items, "range": {"from": today.isoformat(), "to": cutoff.isoformat()}}


def _tool_overdue_charges(db: Session, user: AppUser, project_id: uuid.UUID) -> dict:
    from datetime import date
    today = date.today()
    rows = (
        db.query(Charge)
        .filter(
            Charge.project_id == project_id,
            Charge.due_date < today,
            Charge.status.notin_([ChargeStatus.paid.value, ChargeStatus.cancelled.value]),
        )
        .order_by(Charge.due_date.asc())
        .limit(100)
        .all()
    )
    items = []
    for c in rows:
        tn = db.query(Tenant).filter(Tenant.id == c.tenant_id).first()
        items.append({
            "charge_type": c.charge_type.value if hasattr(c.charge_type, "value") else str(c.charge_type),
            "amount_due": float(c.amount_due),
            "due_date": c.due_date.isoformat() if c.due_date else None,
            "tenant": tn.tenant_name if tn else None,
        })
    return {"row_count": len(items), "items": items, "as_of": today.isoformat()}


# ---------- 按需生成临时透视表（read-only） ----------

TEMP_VIEWS: dict[str, dict[str, Any]] = {
    "tenant_revenue_ranking": {
        "title": "租户营收排行",
        "description": "按已分配收款金额降序列出所有租户及其租约数、应收、已收、余额。",
    },
    "space_revenue": {
        "title": "空间营收贡献",
        "description": "按空间汇总关联租约的应收与已分配收款金额。",
    },
    "monthly_revenue": {
        "title": "月度营收趋势",
        "description": "按 payment_date 月份聚合已完成的收款金额。",
    },
    "charge_aging": {
        "title": "费用账龄分布",
        "description": "把未结清费用按逾期天数分桶：未到期 / 0-30 / 31-60 / 61-90 / 90+ 天。",
    },
    "lease_expiry_calendar": {
        "title": "合同到期日历",
        "description": "按未来 N=180 天按月分组统计即将到期的租约数和总月租金。",
    },
}


def _tool_create_temp_view(
    db: Session, user: AppUser, project_id: uuid.UUID,
    view_type: str, top: int = 20,
) -> dict:
    """根据 view_type 动态生成只读透视表，结果可被前端表格组件直接渲染或导出。

    允许的 view_type 受 TEMP_VIEWS 控制，禁止任意 SQL。
    """
    from datetime import date
    today = date.today()

    if view_type not in TEMP_VIEWS:
        return {"error": f"不支持的视图类型: {view_type}", "supported": list(TEMP_VIEWS.keys())}

    pid = project_id
    if view_type == "tenant_revenue_ranking":
        # 聚合每个租户的已分配收款 + 应收
        rows = (
            db.query(
                Tenant.id,
                Tenant.tenant_name,
                sa_func.count(Lease.id.distinct()).label("lease_count"),
                sa_func.coalesce(sa_func.sum(Charge.amount_due), 0).label("total_charges"),
            )
            .outerjoin(Lease, Lease.tenant_id == Tenant.id)
            .outerjoin(Charge, Charge.tenant_id == Tenant.id)
            .filter(Tenant.project_id == pid)
            .group_by(Tenant.id, Tenant.tenant_name)
            .limit(top)
            .all()
        )
        # 单独查已分配收款（避免双 GROUP BY）
        tenant_ids = [r.id for r in rows]
        paid_by_tenant = dict(
            db.query(Charge.tenant_id, sa_func.coalesce(sa_func.sum(PaymentAllocation.allocated_amount), 0))
            .join(Payment, Payment.id == PaymentAllocation.payment_id)
            .filter(
                Charge.tenant_id.in_(tenant_ids) if tenant_ids else False,
                Payment.status == "completed",
            )
            .group_by(Charge.tenant_id).all()
        )
        items = []
        for r in rows:
            chg = float(r.total_charges or 0)
            paid = float(paid_by_tenant.get(r.id, 0) or 0)
            items.append({
                "租户名称": r.tenant_name,
                "租约数": r.lease_count,
                "应收总额": round(chg, 2),
                "已收总额": round(paid, 2),
                "未收余额": round(chg - paid, 2),
            })
        items.sort(key=lambda x: -x["已收总额"])
        return {
            "view_type": view_type,
            "title": TEMP_VIEWS[view_type]["title"],
            "row_count": len(items),
            "items": items,
            "exportable": True,
        }

    if view_type == "space_revenue":
        rows = (
            db.query(
                Space.id,
                Space.space_code,
                Space.floor,
                sa_func.coalesce(sa_func.sum(Charge.amount_due), 0).label("total_charges"),
            )
            .outerjoin(Lease, Lease.space_id == Space.id)
            .outerjoin(Charge, Charge.lease_id == Lease.id)
            .filter(Space.project_id == pid)
            .group_by(Space.id, Space.space_code, Space.floor)
            .order_by(sa_func.coalesce(sa_func.sum(Charge.amount_due), 0).desc())
            .limit(top)
            .all()
        )
        items = [{
            "空间编号": r.space_code,
            "楼层": r.floor,
            "应收总额": round(float(r.total_charges or 0), 2),
        } for r in rows]
        return {
            "view_type": view_type,
            "title": TEMP_VIEWS[view_type]["title"],
            "row_count": len(items),
            "items": items,
            "exportable": True,
        }

    if view_type == "monthly_revenue":
        # payment_date 按月聚合 completed 收款
        from sqlalchemy import extract as sa_extract
        rows = (
            db.query(
                sa_extract("year", Payment.payment_date).label("year"),
                sa_extract("month", Payment.payment_date).label("month"),
                sa_func.coalesce(sa_func.sum(Payment.amount), 0).label("amount"),
                sa_func.count(Payment.id).label("count"),
            )
            .filter(
                Payment.project_id == pid,
                Payment.status == "completed",
                Payment.payment_date.isnot(None),
            )
            .group_by("year", "month")
            .order_by("year", "month")
            .all()
        )
        items = [{
            "月份": f"{int(r.year):04d}-{int(r.month):02d}",
            "金额": round(float(r.amount or 0), 2),
            "收款笔数": r.count,
        } for r in rows]
        return {
            "view_type": view_type,
            "title": TEMP_VIEWS[view_type]["title"],
            "row_count": len(items),
            "items": items,
            "exportable": True,
        }

    if view_type == "charge_aging":
        # 5 个账龄桶
        rows = (
            db.query(Charge)
            .filter(
                Charge.project_id == pid,
                Charge.status.notin_(["paid", "cancelled"]),
            )
            .all()
        )
        buckets = [
            ("未到期", 0, 0, []),       # due_date >= today
            ("0-30天", 1, 30, []),
            ("31-60天", 31, 60, []),
            ("61-90天", 61, 90, []),
            ("90+天", 91, 99999, []),
        ]
        bucket_dict = {b[0]: {"count": 0, "amount": 0.0} for b in buckets}
        for c in rows:
            if not c.due_date:
                continue
            overdue_days = (today - c.due_date).days
            amount = float(c.amount_due or 0)
            if overdue_days <= 0:
                bucket_dict["未到期"]["count"] += 1
                bucket_dict["未到期"]["amount"] += amount
            elif overdue_days <= 30:
                bucket_dict["0-30天"]["count"] += 1
                bucket_dict["0-30天"]["amount"] += amount
            elif overdue_days <= 60:
                bucket_dict["31-60天"]["count"] += 1
                bucket_dict["31-60天"]["amount"] += amount
            elif overdue_days <= 90:
                bucket_dict["61-90天"]["count"] += 1
                bucket_dict["61-90天"]["amount"] += amount
            else:
                bucket_dict["90+天"]["count"] += 1
                bucket_dict["90+天"]["amount"] += amount
        items = [
            {"账龄": k, "笔数": v["count"], "金额": round(v["amount"], 2)}
            for k, v in bucket_dict.items()
        ]
        return {
            "view_type": view_type,
            "title": TEMP_VIEWS[view_type]["title"],
            "row_count": len(items),
            "items": items,
            "as_of": today.isoformat(),
            "exportable": True,
        }

    if view_type == "lease_expiry_calendar":
        from datetime import timedelta
        horizon = today + timedelta(days=180)
        rows = (
            db.query(Lease)
            .filter(
                Lease.project_id == pid,
                Lease.end_date >= today,
                Lease.end_date <= horizon,
                Lease.status.in_(["active", "pending"]),
            )
            .all()
        )
        monthly: dict[str, dict[str, Any]] = {}
        for l in rows:
            key = l.end_date.strftime("%Y-%m") if l.end_date else "未知"
            entry = monthly.setdefault(key, {"lease_count": 0, "total_rent": 0.0})
            entry["lease_count"] += 1
            entry["total_rent"] += float(l.rent_amount or 0)
        items = [
            {"月份": k, "租约数": v["lease_count"], "租金合计": round(v["total_rent"], 2)}
            for k, v in sorted(monthly.items())
        ]
        return {
            "view_type": view_type,
            "title": TEMP_VIEWS[view_type]["title"],
            "row_count": len(items),
            "items": items,
            "as_of": today.isoformat(),
            "horizon_days": 180,
            "exportable": True,
        }

    return {"error": "unreachable"}


# ---------- LLM-enhanced intent + summary ----------

def _try_llm_intent(message: str, business_context: dict) -> tuple[str, dict] | None:
    """If LLM is available and the rule-based match was weak, ask LLM to pick a tool.

    Returns (tool_name, parsed_args) or None if LLM is unavailable/declined.
    Args dict may contain: days, status, search_text, date_range, entity_type.
    """
    if not (_llm_ok and llm_provider and llm_provider.is_configured()):
        return None

    system = """你是数据查询助手的意图路由器。根据用户的中文问题，选1个最合适的受控工具。
只返回 JSON，不要任何解释文字。允许的工具：
- get_due_leases(days=N) → N 天内到期的租赁合同
- get_overdue_charges() → 逾期未缴费用
- get_business_metrics() → 出租率、活跃租约数、应收款汇总
- search_spaces(status?, search?) → 搜索空间（status=leased/available/reserved）
- get_available_tables() → 各类数据表行数
- create_temp_view(view_type=top|tenant_revenue_ranking|space_revenue|monthly_revenue|charge_aging|lease_expiry_calendar) → 按需生成临时透视表（可导出）

返回格式：{"tool": "xxx", "args": {...}}"""

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"用户问题：{message}"},
    ]

    try:
        raw = llm_provider.chat(messages, temperature=0.0, max_tokens=1200,
                                business_context=business_context)
        # Extract JSON — LLM might wrap in ```json fences
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            raw = raw.rsplit("```", 1)[0]
        parsed = json.loads(raw)
        tool = parsed.get("tool", "")
        args = parsed.get("args", {}) or {}
        valid_tools = {
            "get_due_leases", "get_overdue_charges", "get_business_metrics",
            "search_spaces", "get_available_tables", "create_temp_view",
        }
        if tool in valid_tools:
            return tool, args
        return None
    except Exception:
        return None


# ---------- Intent router ----------

def route_intent(message: str, db: Session, user: AppUser, project_id: uuid.UUID) -> tuple[str, Any, str]:
    """Hybrid rule-based + LLM intent router. Returns (summary, structured_data, tool_name).

    Rule-based handles the 5 common patterns directly (fast, no LLM needed).
    When the message doesn't match any rule clearly, and LLM is available,
    we ask LLM to pick a tool. If LLM also declines → fallback message.
    """
    message.lower()

    # Rule-based fast paths (always try first)
    # 到期合同
    if any(k in message for k in ("到期", "即将到期", "due", "expiring")) and any(k in message for k in ("合同", "lease", "租约")):
        days = 30
        for d in (7, 15, 30, 60, 90, 180, 365):
            if f"{d}" in message or f"{d}天" in message:
                days = d
                break
        data = _tool_due_leases(db, user, project_id, days)
        summary = f"未来 {days} 天内到期合同共 {data['row_count']} 份。"
        return summary, data, "get_due_leases"

    # 逾期费用
    if any(k in message for k in ("逾期", "欠费", "overdue", "欠缴")):
        data = _tool_overdue_charges(db, user, project_id)
        summary = f"当前项目逾期费用共 {data['row_count']} 笔。"
        return summary, data, "get_overdue_charges"

    # 出租率 / 指标
    if any(k in message for k in ("出租率", "occupancy", "指标", "统计", "metric", "数据概览")):
        data = _tool_get_business_metrics(db, user, project_id)
        o = data.get("occupancy", {})
        summary = (
            f"数量出租率 {o.get('space_count_occupancy', {}).get('ratio_pct', 'N/A')}%、"
            f"面积出租率 {o.get('area_occupancy', {}).get('ratio_pct', 'N/A')}%、"
            f"活跃租约 {o.get('active_lease_count', 'N/A')} 份。"
        )
        return summary, data, "get_business_metrics"

    # 空间查询
    if any(k in message for k in ("空间", "房间", "space", "空置", "办公")):
        filters = {}
        for sk, sv in (("available", "available"), ("leased", "leased"), ("待租", "available"), ("已租", "leased"), ("空置", "available")):
            if sk in message:
                filters["status"] = sv
                break
        data = _tool_search_spaces(db, user, project_id, filters)
        summary = f"符合条件的空间共 {data['row_count']} 个。"
        return summary, data, "search_spaces"

    # 可用表
    if any(k in message for k in ("有什么表", "数据表", "有哪些数据", "table", "数据分类")):
        data = _tool_get_available_tables(db, user, project_id)
        summary = f"当前项目有 {len(data['tables'])} 类数据（已跳过空表）。"
        return summary, data, "get_available_tables"

    # 按需生成临时透视表（规则匹配）
    temp_view_kws = {
        "tenant_revenue_ranking": ("租户营收", "营收排行", "租户排行", "客户排行"),
        "space_revenue": ("空间营收", "房间营收", "空间贡献", "空间排行"),
        "monthly_revenue": ("月度营收", "月度趋势", "按月营收", "月度收入"),
        "charge_aging": ("账龄", "费用账龄", "逾期分布", "账龄分析"),
        "lease_expiry_calendar": ("到期日历", "合同到期日历", "续约提醒", "续约日历"),
    }
    for vt, kws in temp_view_kws.items():
        if any(kw in message for kw in kws):
            data = _tool_create_temp_view(db, user, project_id, vt, top=20)
            if "error" in data:
                return data["error"], data, "fallback"
            summary = (
                f"已生成临时视图「{data['title']}」共 {data['row_count']} 行。"
                "可点击表格右上角「导出 Excel / CSV」下载。"
            )
            return summary, data, "create_temp_view"

    # Rule-based didn't match — try LLM intent router
    business_ctx = {"project_id": str(project_id)}
    llm_pick = _try_llm_intent(message, business_ctx)
    if llm_pick:
        tool_name, args = llm_pick
        if tool_name == "get_due_leases":
            days = int(args.get("days", 30))
            data = _tool_due_leases(db, user, project_id, days)
            summary = f"未来 {days} 天内到期合同共 {data['row_count']} 份。"
            return summary, data, tool_name
        if tool_name == "get_overdue_charges":
            data = _tool_overdue_charges(db, user, project_id)
            summary = f"当前逾期费用共 {data['row_count']} 笔。"
            return summary, data, tool_name
        if tool_name == "get_business_metrics":
            data = _tool_get_business_metrics(db, user, project_id)
            o = data.get("occupancy", {})
            summary = (
                f"数量出租率 {o.get('space_count_occupancy', {}).get('ratio_pct', 'N/A')}%、"
                f"面积出租率 {o.get('area_occupancy', {}).get('ratio_pct', 'N/A')}%。"
            )
            return summary, data, tool_name
        if tool_name == "search_spaces":
            filters = {}
            if args.get("status"):
                filters["status"] = args["status"]
            if args.get("search"):
                filters["search"] = args["search"]
            data = _tool_search_spaces(db, user, project_id, filters)
            summary = f"符合条件的空间共 {data['row_count']} 个。"
            return summary, data, tool_name
        if tool_name == "get_available_tables":
            data = _tool_get_available_tables(db, user, project_id)
            summary = f"当前项目有 {len(data['tables'])} 类数据。"
            return summary, data, tool_name
        if tool_name == "create_temp_view":
            vt = args.get("view_type") or "tenant_revenue_ranking"
            top = int(args.get("top", 20))
            data = _tool_create_temp_view(db, user, project_id, vt, top=top)
            if "error" in data:
                summary = data["error"]
            else:
                summary = (
                    f"已生成临时视图「{data.get('title', vt)}」共 {data['row_count']} 行。"
                    "可在聊天界面表格中导出 Excel/CSV。"
                )
            return summary, data, tool_name

    # Fallback
    if _llm_ok and llm_provider and llm_provider.is_configured():
        summary = "已收到查询，但未能明确匹配到受控工具。建议使用：到期合同 / 逾期费用 / 出租率 / 空间 等关键词。"
    else:
        summary = "系统尚未配置大模型（LLM API Key）。请使用明确的关键词：到期合同 / 逾期费用 / 出租率 / 空间查询 / 数据分类。"

    return summary, {"hint": "try: 未来30天到期合同"}, "fallback"


# ---------- Endpoints ----------

@router.post("/conversations")
def create_conversation(payload: NewConversation, db: Session = Depends(get_db), user: AppUser = Depends(CurrentUser())):
    if not require_project_scope(payload.project_id, user, db):
        raise HTTPException(status_code=403, detail="无权访问此项目。")
    conv = AIConversation(
        id=uuid.uuid4(),
        project_id=uuid.UUID(payload.project_id),
        user_id=user.id,
        title=payload.title or "新对话",
    )
    db.add(conv)
    db.commit()
    return {"id": str(conv.id), "title": conv.title}


@router.get("/conversations")
def list_conversations(db: Session = Depends(get_db), user: AppUser = Depends(CurrentUser())):
    rows = (
        db.query(AIConversation)
        .filter(AIConversation.user_id == user.id)
        .order_by(AIConversation.updated_at.desc())
        .all()
    )
    return {"items": [{"id": str(c.id), "title": c.title, "updated_at": c.updated_at.isoformat()} for c in rows]}


@router.get("/conversations/{conv_id}")
def get_conversation(conv_id: str, db: Session = Depends(get_db), user: AppUser = Depends(CurrentUser())):
    try:
        cid = uuid.UUID(conv_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的会话 ID。")
    conv = db.query(AIConversation).filter(AIConversation.id == cid).first()
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在。")
    if conv.user_id != user.id and not user.is_system_admin:
        raise HTTPException(status_code=403, detail="无权访问。")

    msgs = db.query(AIMessage).filter(AIMessage.conversation_id == cid).order_by(AIMessage.created_at.asc()).all()
    return {
        "conversation": {"id": str(conv.id), "title": conv.title},
        "messages": [
            {
                "id": str(m.id),
                "role": m.role,
                "content": m.content,
                "tool_name": m.tool_name,
                "tool_args_safe": m.tool_args_safe,
                "created_at": m.created_at.isoformat(),
            }
            for m in msgs
        ],
    }


# ---------- 直接调用受控工具端点（供前端表格组件使用） ----------

@router.get("/tools/temp-views")
def list_temp_views(
    db: Session = Depends(get_db),
    user: AppUser = Depends(CurrentUser()),
) -> dict:
    """列出所有可用的临时视图类型，供前端下拉/快捷入口使用。"""
    return {
        "items": [
            {"view_type": vt, "title": meta["title"], "description": meta["description"]}
            for vt, meta in TEMP_VIEWS.items()
        ]
    }


@router.post("/tools/temp-views/{view_type}")
def invoke_temp_view(
    view_type: str,
    project_id: str,
    top: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    user: AppUser = Depends(CurrentUser()),
) -> dict:
    """直接调用 create_temp_view 工具（不经 LLM 路由），用于前端"按需生成表格"按钮。"""
    if not require_project_scope(project_id, user, db):
        raise HTTPException(status_code=403, detail="无权访问此项目。")
    return _tool_create_temp_view(db, user, uuid.UUID(project_id), view_type, top)


@router.post("/conversations/{conv_id}/messages")
def send_message(conv_id: str, payload: SendMessage, req: Request, db: Session = Depends(get_db), user: AppUser = Depends(CurrentUser())):
    """Send a message → intent routing → tool call → structured response."""
    try:
        cid = uuid.UUID(conv_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的会话 ID。")
    conv = db.query(AIConversation).filter(AIConversation.id == cid).first()
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在。")
    if conv.user_id != user.id:
        raise HTTPException(status_code=403, detail="无权访问此会话。")
    if not require_project_scope(str(conv.project_id), user, db):
        raise HTTPException(status_code=403, detail="无权访问此项目。")

    # Save user message
    user_msg = AIMessage(
        id=uuid.uuid4(),
        conversation_id=cid,
        role="user",
        content=payload.content,
    )
    db.add(user_msg)
    db.flush()

    # Intent → tool
    start = datetime.now(timezone.utc)
    summary, data, tool_name = route_intent(payload.content, db, user, conv.project_id)
    latency_ms = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)

    # Try LLM polish of summary (non-blocking — failure just keeps rule-based summary)
    llm_polish = None
    if _llm_ok and llm_provider and llm_provider.is_configured() and tool_name != "fallback":
        try:
            polished = llm_provider.summarize_tool_result(tool_name, data, payload.content)
            if polished and len(polished) > 5 and polished != summary:
                llm_polish = polished
                summary = polished
        except Exception:
            pass  # keep rule-based summary

    # Save tool run
    tool_run = AIToolRun(
        id=uuid.uuid4(),
        conversation_id=cid,
        message_id=user_msg.id,
        tool_name=tool_name,
        status="ok",
        latency_ms=latency_ms,
        row_count=data.get("row_count", len(data.get("items", []))) if isinstance(data, dict) else None,
        actor_user_id=user.id,
    )
    db.add(tool_run)

    # Save assistant message with structured data (truncate if huge)

    assistant_msg = AIMessage(
        id=uuid.uuid4(),
        conversation_id=cid,
        role="assistant",
        content=summary,
        tool_name=tool_name,
        tool_args_safe={
            "summary": summary[:500],
            "metric_definitions": {
                "出租率": "数量出租率 = 已租空间数 / 可出租空间总数（排除停用）",
                "逾期": "due_date < 今天 且 费用状态非 paid/cancelled",
                "到期": "合同结束日期在指定天数内，状态非 terminated",
            },
            "warnings": [] if tool_name != "fallback" else ["未能明确理解意图，请使用具体关键词。"],
            "source": f"project:{conv.project_id}",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    db.add(assistant_msg)

    conv.updated_at = datetime.now(timezone.utc)
    db.commit()

    return {
        "message": summary,
        "tool": tool_name,
        "llm_polished": llm_polish is not None,
        "latency_ms": latency_ms,
        "data": data,
        "metric_definitions": assistant_msg.tool_args_safe["metric_definitions"],
        "export_available": True,
        "source": f"project:{conv.project_id}",
    }


# ---------- Streaming (SSE) ----------

# SSE event protocol:
#   event: meta             data: {"tool": "...", "row_count": N?, "range": {...}?}
#   event: summary_delta   data: {"text": "..."}     (LLM final summary chunk)
#   event: reasoning_delta data: {"text": "..."}     (LLM thinking chunk, optional)
#   event: done             data: {"message_id": "...", "summary": "...",
#                                  "tool": "...", "latency_ms": N,
#                                  "llm_polished": bool, "row_count": N?,
#                                  "warnings": [...], "metric_definitions": {...}}
#   event: error            data: {"message": "..."}
#
# Frontend consumes via ReadableStream and EventSource-style parser; each event
# arrives as a JSON object so the page can render deltas in real time.

def _sse(event: str, data: dict | str) -> bytes:
    """Encode one SSE frame as bytes (UTF-8).

    `event` is the SSE event name. `data` is either a dict (serialised) or a
    pre-formatted string (must already be valid JSON).
    """
    if isinstance(data, dict):
        payload = json.dumps(data, ensure_ascii=False, default=str)
    else:
        payload = data
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


def _safe_truncate(s: str, n: int = 3000) -> str:
    if len(s) <= n:
        return s
    return s[:n] + "…(截断)"


def _stream_summarize_tool_result(
    tool_name: str,
    data: dict,
    user_query: str,
) -> tuple[Iterator[tuple[str, str]], bool]:
    """Stream the LLM summary of a tool result.

    Returns (iterator_of_(kind, text)_tuples, llm_was_used_flag).
    `kind` is "content" or "reasoning".
    Falls back to a single rule-based chunk if LLM is unavailable.
    """
    if _llm_ok and llm_provider and llm_provider.is_configured():
        # Build a streaming chat call mirroring summarize_tool_result
        try:
            safe_data = json.dumps(data, ensure_ascii=False)
            safe_data = _safe_truncate(safe_data, 3000)
            messages = [
                {
                    "role": "user",
                    "content": (
                        f"用户原查询：{user_query}\n\n"
                        f"工具：{tool_name}\n"
                        f"工具返回（JSON）：{safe_data}\n\n"
                        "请用中文写一段简洁的业务总结（2-4 句），突出关键数字和结论。"
                        "如果数据为空也要如实说明。"
                    ),
                },
            ]

            def _wrapped() -> Iterator[tuple[str, str]]:
                for raw in llm_provider.chat_stream(messages, temperature=0.2, max_tokens=1024):
                    if raw.startswith("[content]"):
                        yield ("content", raw[len("[content]"):])
                    elif raw.startswith("[reasoning]"):
                        yield ("reasoning", raw[len("[reasoning]"):])

            return _wrapped(), True
        except Exception:
            pass

    # Fallback single-chunk summary
    fallback_text = _rule_based_summary(tool_name, data)

    def _fallback() -> Iterator[tuple[str, str]]:
        yield ("content", fallback_text)

    return _fallback(), False


def _rule_based_summary(tool_name: str, data: dict) -> str:
    """Mirror summarise_tool_result fallback branch — pure rule-based, no LLM."""
    if tool_name == "get_due_leases":
        return f"未来到期合同共 {data.get('row_count', 0)} 份。"
    if tool_name == "get_overdue_charges":
        return f"当前逾期费用共 {data.get('row_count', 0)} 笔。"
    if tool_name == "get_business_metrics":
        o = data.get("occupancy", {})
        return (
            f"数量出租率 {o.get('space_count_occupancy', {}).get('ratio_pct', 'N/A')}%、"
            f"面积出租率 {o.get('area_occupancy', {}).get('ratio_pct', 'N/A')}%、"
            f"活跃租约 {o.get('active_lease_count', 'N/A')} 份。"
        )
    return f"工具 {tool_name} 返回数据。"


@router.post("/conversations/{conv_id}/stream")
def send_message_stream(
    conv_id: str,
    payload: SendMessage,
    req: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(CurrentUser()),
) -> StreamingResponse:
    """Streaming variant: Server-Sent Events (text/event-stream).

    Performs full intent routing + tool call up front (sync), then streams the
    natural-language summary as it arrives from the LLM. Falls back to a
    single rule-based summary chunk when LLM is unavailable. Persists user +
    assistant messages and tool run + audit log on completion.
    """
    try:
        cid = uuid.UUID(conv_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的会话 ID。")

    conv = db.query(AIConversation).filter(AIConversation.id == cid).first()
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在。")
    if conv.user_id != user.id:
        raise HTTPException(status_code=403, detail="无权访问此会话。")
    if not require_project_scope(str(conv.project_id), user, db):
        raise HTTPException(status_code=403, detail="无权访问此项目。")

    # Persist user message immediately (so a network drop still keeps history)
    user_msg = AIMessage(
        id=uuid.uuid4(),
        conversation_id=cid,
        role="user",
        content=payload.content,
    )
    db.add(user_msg)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="用户消息持久化失败")

    # Intent routing — compute structured data + tool name up front
    start = datetime.now(timezone.utc)
    summary_initial, data, tool_name = route_intent(payload.content, db, user, conv.project_id)
    latency_ms = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)

    # Pre-build meta payload (so the UI can render the tool label early)
    meta_payload: dict[str, Any] = {
        "tool": tool_name,
        "user_message_id": str(user_msg.id),
        "latency_ms": latency_ms,
    }
    if isinstance(data, dict):
        if "row_count" in data:
            meta_payload["row_count"] = data["row_count"]
        if "range" in data:
            meta_payload["range"] = data["range"]
        if "exportable" in data:
            meta_payload["exportable"] = data["exportable"]

    def event_iter():
        # 1) meta frame so the UI can show the tool pill immediately
        yield _sse("meta", meta_payload)

        # Decide whether to stream LLM summary (only when there's something
        # meaningful to polish and the LLM is configured)
        can_stream = (
            tool_name != "fallback"
            and _llm_ok
            and llm_provider is not None
            and llm_provider.is_configured()
        )

        accumulated: list[str] = []
        llm_used = False

        try:
            if can_stream:
                stream_it, llm_used_inner = _stream_summarize_tool_result(
                    tool_name, data, payload.content
                )
                llm_used = llm_used_inner
                for kind, text in stream_it:
                    if not text:
                        continue
                    if kind == "content":
                        accumulated.append(text)
                        yield _sse("summary_delta", {"text": text})
                    elif kind == "reasoning":
                        yield _sse("reasoning_delta", {"text": text})
            else:
                # No LLM: emit rule-based fallback as one chunk
                summary_text = _rule_based_summary(tool_name, data) if tool_name != "fallback" else summary_initial
                if summary_text:
                    accumulated.append(summary_text)
                    yield _sse("summary_delta", {"text": summary_text})
        except Exception as exc:
            # Streaming failure → fall back to a single rule-based chunk so
            # the user still gets a response
            if not accumulated:
                fallback = _rule_based_summary(tool_name, data) if tool_name != "fallback" else summary_initial
                if fallback:
                    accumulated.append(fallback)
                    yield _sse("summary_delta", {"text": fallback})
            yield _sse("error", {"message": f"LLM 流式响应失败: {type(exc).__name__}"})

        # Final accumulated summary text (concatenate all content chunks)
        final_summary = "".join(accumulated).strip() or summary_initial

        # Persist tool run + assistant message + audit log
        try:
            tool_run = AIToolRun(
                id=uuid.uuid4(),
                conversation_id=cid,
                message_id=user_msg.id,
                tool_name=tool_name,
                status="ok",
                latency_ms=latency_ms,
                row_count=data.get("row_count", len(data.get("items", []))) if isinstance(data, dict) else None,
                actor_user_id=user.id,
            )
            db.add(tool_run)

            assistant_msg = AIMessage(
                id=uuid.uuid4(),
                conversation_id=cid,
                role="assistant",
                content=final_summary,
                tool_name=tool_name,
                tool_args_safe={
                    "summary": final_summary[:500],
                    "metric_definitions": {
                        "出租率": "数量出租率 = 已租空间数 / 可出租空间总数（排除停用）",
                        "逾期": "due_date < 今天 且 费用状态非 paid/cancelled",
                        "到期": "合同结束日期在指定天数内，状态非 terminated",
                    },
                    "warnings": [] if tool_name != "fallback" else ["未能明确理解意图，请使用具体关键词。"],
                    "source": f"project:{conv.project_id}",
                    "streamed": True,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            db.add(assistant_msg)
            conv.updated_at = datetime.now(timezone.utc)

            try:
                audit = AuditLog(
                    id=uuid.uuid4(),
                    organization_id=conv.organization_id,
                    project_id=conv.project_id,
                    actor_user_id=user.id,
                    action="assistant.stream",
                    resource_type="conversation",
                    resource_id=str(cid),
                    after_summary={
                        "tool": tool_name,
                        "latency_ms": latency_ms,
                        "llm_polished": llm_used,
                        "streamed": True,
                    },
                    ip_address=req.client.host if req and req.client else None,
                    request_id=getattr(req.state, "request_id", None) if req else None,
                )
                db.add(audit)
            except Exception:
                # Audit log is best-effort; never fail the user response
                pass

            db.commit()
        except Exception:
            # Persistence failure — surface as event but still emit done
            try:
                db.rollback()
            except Exception:
                pass
            yield _sse("error", {"message": "结果持久化失败，但响应已生成。"})

        # 2) Done frame so the frontend can swap the streaming bubble to a
        # finalised message + show the tool meta footer.
        yield _sse("done", {
            "message_id": str(assistant_msg.id) if 'assistant_msg' in locals() else None,
            "tool": tool_name,
            "summary": final_summary,
            "latency_ms": latency_ms,
            "llm_polished": llm_used,
            "row_count": (data.get("row_count") if isinstance(data, dict) else None),
            "metric_definitions": {
                "出租率": "数量出租率 = 已租空间数 / 可出租空间总数（排除停用）",
                "逾期": "due_date < 今天 且 费用状态非 paid/cancelled",
                "到期": "合同结束日期在指定天数内，状态非 terminated",
            },
            "warnings": [] if tool_name != "fallback" else ["未能明确理解意图，请使用具体关键词。"],
        })

    return StreamingResponse(
        event_iter(),
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # disable nginx buffering if any reverse proxy
            "Connection": "keep-alive",
        },
    )

