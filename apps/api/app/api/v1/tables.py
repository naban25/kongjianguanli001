"""Generic table endpoint — any import sheet or business entity as a "table".

V1 supports:
  - ImportSheet raw values (保留用户原始结构)
  - Business entities: spaces | tenants | leases | charges | payments

All operations respect project-level scope + role checks.
"""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_, asc, desc, Numeric, Integer, Float, SmallInteger, BigInteger, Date, DateTime as DateTimeType
from sqlalchemy.orm import Session

from ...core.auth import CurrentUser, require_project_scope
from ...core.config import get_settings
from ...core.database import get_db
from ...core.org_context import ensure_project_in_active_org
from ...models import AppUser, AuditLog, ImportRow, ImportSheet, Project
from ... import models as M
from ...services.apitable_sync import APITableError, sync_entity

router = APIRouter(prefix="/tables", tags=["tables"])
logger = logging.getLogger(__name__)


# ---------- Column metadata (Chinese labels + display hints) ----------

COLUMN_META: dict[str, list[dict]] = {
    "spaces": [
        {"name": "space_code", "label": "空间编号", "type": "text"},
        {"name": "floor", "label": "楼层", "type": "number"},
        {"name": "area_sqm", "label": "面积(㎡)", "type": "number"},
        {"name": "workstation_count", "label": "工位数", "type": "number"},
        {"name": "space_type", "label": "空间类型", "type": "enum", "enum_map": {
            "office": "办公室", "meeting": "会议室", "storage": "仓库",
            "coworking": "联合办公", "retail": "商铺", "other": "其他",
        }},
        {"name": "status", "label": "状态", "type": "enum", "enum_map": {
            "available": "待租", "reserved": "预留", "leased": "已租",
            "inactive": "停用", "unknown": "待确认",
        }},
        {"name": "created_at", "label": "创建时间", "type": "datetime"},
    ],
    "tenants": [
        {"name": "tenant_name", "label": "租户名称", "type": "text"},
        {"name": "tenant_type", "label": "租户类型", "type": "enum", "enum_map": {
            "enterprise": "企业", "individual": "个人", "ngo": "非营利", "other": "其他",
        }},
        {"name": "registration_code", "label": "统一社会信用代码", "type": "text"},
        {"name": "contact_name", "label": "联系人", "type": "text"},
        {"name": "contact_phone_encrypted", "label": "联系电话", "type": "text"},
        {"name": "industry", "label": "行业", "type": "text"},
        {"name": "status", "label": "状态", "type": "enum", "enum_map": {
            "active": "活跃", "inactive": "停用", "blacklisted": "黑名单",
        }},
        {"name": "created_at", "label": "创建时间", "type": "datetime"},
    ],
    "leases": [
        {"name": "lease_code", "label": "合同编号", "type": "text"},
        {"name": "tenant_name", "label": "租户名称", "type": "text", "virtual": True},
        {"name": "space_name", "label": "空间编号", "type": "text", "virtual": True},
        {"name": "contract_signed_date", "label": "签约日期", "type": "date"},
        {"name": "start_date", "label": "起租日期", "type": "date"},
        {"name": "end_date", "label": "到期日期", "type": "date"},
        {"name": "rent_amount", "label": "月租金", "type": "currency"},
        {"name": "deposit_amount", "label": "押金", "type": "currency"},
        {"name": "payment_cycle", "label": "付款周期", "type": "enum", "enum_map": {
            "monthly": "月付", "quarterly": "季付", "half_year": "半年付",
            "yearly": "年付", "one_time": "一次性", "custom": "自定义",
        }},
        {"name": "status", "label": "状态", "type": "enum", "enum_map": {
            "draft": "草稿", "pending": "待生效", "active": "生效中",
            "expired": "已到期", "terminated": "已终止", "vacated": "已退租", "renewed": "已续签",
        }},
        {"name": "created_at", "label": "创建时间", "type": "datetime"},
    ],
    "charges": [
        {"name": "charge_type", "label": "费用类型", "type": "enum", "enum_map": {
            "rent": "租金", "property_fee": "物业费", "water": "水费",
            "electricity": "电费", "internet": "网络费", "parking": "停车费",
            "deposit": "押金", "other": "其他",
        }},
        {"name": "period_start", "label": "计费起", "type": "date"},
        {"name": "period_end", "label": "计费止", "type": "date"},
        {"name": "due_date", "label": "到期日", "type": "date"},
        {"name": "amount_due", "label": "应收金额", "type": "currency"},
        {"name": "status", "label": "状态", "type": "enum", "enum_map": {
            "not_due": "未到期", "unpaid": "待缴", "partial": "部分缴纳",
            "paid": "已结清", "overdue": "逾期", "cancelled": "已取消",
        }},
        {"name": "created_at", "label": "创建时间", "type": "datetime"},
    ],
    "payments": [
        {"name": "payment_date", "label": "收款日期", "type": "date"},
        {"name": "amount", "label": "收款金额", "type": "currency"},
        {"name": "payment_method", "label": "付款方式", "type": "enum", "enum_map": {
            "bank_transfer": "银行转账", "cash": "现金", "check": "支票",
            "online": "在线支付", "other": "其他",
        }},
        {"name": "reference_no", "label": "流水号", "type": "text"},
        {"name": "status", "label": "状态", "type": "enum", "enum_map": {
            "pending": "处理中", "completed": "已完成", "refunded": "已退款", "cancelled": "已取消",
        }},
        {"name": "created_at", "label": "创建时间", "type": "datetime"},
    ],
}

# Enum → badge tone mapping (frontend uses)
ENUM_BADGE_TONE: dict[str, dict[str, str]] = {
    "spaces.status": {"leased": "green", "available": "blue", "reserved": "amber", "inactive": "slate", "unknown": "slate"},
    "tenants.status": {"active": "green", "inactive": "slate", "blacklisted": "red"},
    "leases.status": {"draft": "slate", "pending": "amber", "active": "green", "expired": "slate", "terminated": "red", "vacated": "slate", "renewed": "blue"},
    "charges.status": {"not_due": "slate", "unpaid": "amber", "partial": "amber", "paid": "green", "overdue": "red", "cancelled": "slate"},
    "payments.status": {"pending": "amber", "completed": "green", "refunded": "blue", "cancelled": "slate"},
}

# Columns that are never user-editable
NON_EDITABLE_COLUMNS = {"id", "created_at", "updated_at", "organization_id", "project_id", "version"}


# ---------- Helpers ----------

def _serialize_raw(row: ImportRow) -> dict:
    # Flatten raw_values so the frontend can render row[col_name] directly.
    flat: dict = dict(row.raw_values or {})
    flat["id"] = str(row.id)
    flat["source_row_number"] = row.source_row_number
    flat["validation_status"] = row.validation_status.value if hasattr(row.validation_status, "value") else str(row.validation_status)
    return flat


def _schema_for(entity: str) -> list[dict] | None:
    return COLUMN_META.get(entity)


# ---------- Endpoints ----------

@router.get("")
def list_tables(
    project_id: str = Query(...),
    db: Session = Depends(get_db),
    user: AppUser = Depends(CurrentUser()),
):
    """List all data-table views available to this project: import sheets + business entities."""
    if not require_project_scope(project_id, user, db):
        raise HTTPException(status_code=403, detail="无权访问此项目。")
    pid = uuid.UUID(project_id)
    ensure_project_in_active_org(pid, user, db)

    # Import sheets
    sheets = (
        db.query(ImportSheet)
        .join(ImportRow, ImportRow.import_sheet_id == ImportSheet.id)
        .filter(ImportRow.project_id == pid)
        .distinct()
        .all()
    )
    items = []
    for s in sheets:
        items.append({
            "id": f"import_sheet:{s.id}",
            "kind": "import_sheet",
            "name": s.sheet_name,
            "row_count": s.row_count,
            "column_count": s.column_count,
        })

    # Business entities
    items += [
        {"id": "business:spaces", "kind": "business", "name": "空间", "entity": "spaces"},
        {"id": "business:tenants", "kind": "business", "name": "租户", "entity": "tenants"},
        {"id": "business:leases", "kind": "business", "name": "租赁合同", "entity": "leases"},
        {"id": "business:charges", "kind": "business", "name": "费用台账", "entity": "charges"},
        {"id": "business:payments", "kind": "business", "name": "收款记录", "entity": "payments"},
    ]
    return {"items": items}


@router.get("/{table_ref}/schema")
def get_table_schema(
    table_ref: str,
    db: Session = Depends(get_db),
    user: AppUser = Depends(CurrentUser()),
    project_id: str = Query(...),
):
    """Return column metadata for a business table so frontend can render headers properly."""
    if not require_project_scope(project_id, user, db):
        raise HTTPException(status_code=403, detail="无权访问此项目。")
    ensure_project_in_active_org(uuid.UUID(project_id), user, db)

    if table_ref.startswith("business:"):
        entity = table_ref.split(":", 1)[1]
        cols = COLUMN_META.get(entity)
        if cols is None:
            raise HTTPException(status_code=400, detail=f"未知实体: {entity}")
        # Also include badge tones for enum fields
        schema = []
        for c in cols:
            sc = dict(c)
            if c["type"] == "enum":
                key = f"{entity}.{c['name']}"
                sc["badge_tone"] = ENUM_BADGE_TONE.get(key, {})
            schema.append(sc)
        return {"columns": schema, "entity": entity}

    if table_ref.startswith("import_sheet:"):
        # For raw import sheets, infer columns from the first row's raw_values keys
        sid = uuid.UUID(table_ref.split(":", 1)[1])
        first = (
            db.query(ImportRow)
            .filter(ImportRow.import_sheet_id == sid)
            .order_by(ImportRow.source_row_number.asc())
            .first()
        )
        if first and first.raw_values:
            cols = [{"name": "source_row_number", "label": "行号", "type": "number"}]
            cols += [{"name": str(k), "label": str(k), "type": "text"} for k in first.raw_values.keys()]
            return {"columns": cols, "entity": "import_sheet"}
        return {"columns": [{"name": "source_row_number", "label": "行号", "type": "number"}], "entity": "import_sheet"}

    raise HTTPException(status_code=400, detail="无效的表引用。")


@router.get("/{table_ref}/records")
def get_records(
    table_ref: str,
    project_id: str = Query(...),
    search: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    filters: Optional[str] = None,
    db: Session = Depends(get_db),
    user: AppUser = Depends(CurrentUser()),
):
    if not require_project_scope(project_id, user, db):
        raise HTTPException(status_code=403, detail="无权访问此项目。")
    pid = uuid.UUID(project_id)
    ensure_project_in_active_org(pid, user, db)

    if table_ref.startswith("import_sheet:"):
        sid = uuid.UUID(table_ref.split(":", 1)[1])
        q = db.query(ImportRow).filter(ImportRow.import_sheet_id == sid, ImportRow.project_id == pid)
        if search:
            q = q.filter(ImportRow.raw_values.cast(db.bind.dialect.text_type).ilike(f"%{search}%"))
        total = q.count()
        rows = q.order_by(ImportRow.source_row_number.asc()).offset((page - 1) * page_size).limit(page_size).all()
        return {"items": [_serialize_raw(r) for r in rows], "total": total, "page": page, "page_size": page_size}

    if table_ref.startswith("business:"):
        entity = table_ref.split(":", 1)[1]
        return _business_records(db, pid, entity, page, page_size, search, sort_by, sort_dir, filters)

    raise HTTPException(status_code=400, detail="无效的表引用。")


def _parse_filters(filters_str: str | None) -> list[tuple[str, str, str]]:
    """Parse 'field:op:value,field2:op:val2' into [(field, op, value), ...].

    支持的操作符：
      eq / ne / gt / gte / lt / lte / like / in
      between — value 形式 'min|max'，对数字与日期列做闭区间 [min, max]
    """
    if not filters_str:
        return []
    ops = {"eq", "ne", "gt", "gte", "lt", "lte", "like", "in", "between"}
    parsed: list[tuple[str, str, str]] = []
    for part in filters_str.split(","):
        parts = part.strip().split(":", 2)
        if len(parts) != 3:
            continue
        field, op, value = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if op not in ops:
            continue
        if op == "between" and "|" not in value:
            continue
        parsed.append((field, op, value))
    return parsed


def _safe_num(value: str) -> float | str:
    try:
        return float(value)
    except ValueError:
        return value


def _business_records(db: Session, pid: uuid.UUID, entity: str, page: int, page_size: int,
                      search: Optional[str], sort_by: Optional[str], sort_dir: str,
                      filters: Optional[str] = None):
    from ... import models as M

    model_map = {
        "spaces": M.Space,
        "tenants": M.Tenant,
        "leases": M.Lease,
        "charges": M.Charge,
        "payments": M.Payment,
    }
    ModelClass = model_map.get(entity)
    if ModelClass is None:
        raise HTTPException(status_code=400, detail=f"未知实体: {entity}")

    q = db.query(ModelClass).filter(ModelClass.project_id == pid)

    # Search on string columns only
    if search:
        from sqlalchemy import String as SAString, Enum as SAEnum
        # str(c.type) for VARCHAR(n) renders as "VARCHAR(n)" — match by class, not name.
        # SA Enum subclasses String but ILIKE on a pg enum errors out — exclude it.
        string_cols = [c.name for c in ModelClass.__table__.columns
                       if isinstance(c.type, SAString) and not isinstance(c.type, SAEnum)]
        if string_cols:
            q = q.filter(or_(*[getattr(ModelClass, c).ilike(f"%{search}%") for c in string_cols]))

    # Apply column-level filters
    col_names = {c.name for c in ModelClass.__table__.columns}
    for field, op, value in _parse_filters(filters):
        if field not in col_names:
            continue
        col = getattr(ModelClass, field)
        # 数值列（Integer/Numeric/Float 等）需要把字符串值转为数字后再比较，
        # 否则 PG 会拒绝 integer_column == '5' 这类跨类型比较（500 INTERNAL_ERROR）。
        from sqlalchemy import Numeric, Integer, Float, SmallInteger, BigInteger
        num_types = (Numeric, Integer, Float, SmallInteger, BigInteger)
        is_numeric = isinstance(col.type, num_types)
        coerced = _safe_num(value) if is_numeric else value
        if op == "eq":
            q = q.filter(col == coerced)
        elif op == "ne":
            q = q.filter(col != coerced)
        elif op == "gt":
            q = q.filter(col > coerced)
        elif op == "gte":
            q = q.filter(col >= coerced)
        elif op == "lt":
            q = q.filter(col < coerced)
        elif op == "lte":
            q = q.filter(col <= coerced)
        elif op == "like":
            q = q.filter(col.ilike(f"%{value}%"))
        elif op == "in":
            q = q.filter(col.in_([v.strip() for v in value.split("|")]))
        elif op == "between":
            a, b = value.split("|", 1)
            # 不同类型列需要显式转换值，否则 PG 会拒绝（date >= VARCHAR 等）
            num_types = (Numeric, Integer, Float, SmallInteger, BigInteger)
            if isinstance(col.type, num_types):
                a_v, b_v = _safe_num(a), _safe_num(b)
            elif isinstance(col.type, Date):
                from datetime import date as _date
                a_v, b_v = _date.fromisoformat(a), _date.fromisoformat(b)
            elif isinstance(col.type, DateTimeType):
                from datetime import datetime as _dt
                a_v = _dt.fromisoformat(a) if "T" in a or " " in a else _dt.fromisoformat(a + "T00:00:00")
                b_v = _dt.fromisoformat(b) if "T" in b or " " in b else _dt.fromisoformat(b + "T23:59:59")
            else:
                a_v, b_v = a, b
            q = q.filter(col >= a_v, col <= b_v)

    total = q.count()

    # Apply sort — validate sort_by against actual columns
    col_names = {c.name for c in ModelClass.__table__.columns}
    if sort_by and sort_by in col_names:
        col = getattr(ModelClass, sort_by)
        q = q.order_by(desc(col) if sort_dir == "desc" else asc(col))
    else:
        q = q.order_by(ModelClass.created_at.desc())

    rows = q.offset((page - 1) * page_size).limit(page_size).all()

    def to_dict(row) -> dict:
        d = {}
        for col in row.__table__.columns:
            val = getattr(row, col.name)
            if isinstance(val, Decimal):
                val = float(val)
            elif hasattr(val, "isoformat"):
                val = val.isoformat()
            elif isinstance(val, uuid.UUID):
                val = str(val)
            elif hasattr(val, "value"):
                val = val.value
            d[col.name] = val
        if entity == "leases":
            t = getattr(row, "tenant", None)
            s = getattr(row, "space", None)
            d["tenant_name"] = t.tenant_name if t else None
            d["space_name"] = (s.space_code if s else None)
        return d

    return {"items": [to_dict(r) for r in rows], "total": total, "page": page, "page_size": page_size}


# ---------- Record edit / delete (business tables only) ----------

class RecordUpdateRequest(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


def _business_model(entity: str):
    from ... import models as M
    model_map = {
        "spaces": M.Space,
        "tenants": M.Tenant,
        "leases": M.Lease,
        "charges": M.Charge,
        "payments": M.Payment,
    }
    ModelClass = model_map.get(entity)
    if ModelClass is None:
        raise HTTPException(status_code=400, detail=f"未知实体: {entity}")
    return ModelClass


def _coerce_value(entity: str, field: str, value: Any, col_meta: dict):
    """Convert an incoming JSON value into the python type the column expects."""
    # Empty string = "clear this field"; NOT NULL columns are rejected in _apply_update
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    t = col_meta.get("type", "text")
    if t == "enum":
        # Accept both English enum values and Chinese labels
        enum_map = col_meta.get("enum_map", {})
        if str(value) in enum_map:
            return str(value)
        zh_to_en = {zh: en for en, zh in enum_map.items()}
        if str(value) in zh_to_en:
            return zh_to_en[str(value)]
        raise HTTPException(status_code=400, detail=f"字段 {field} 的值 '{value}' 不在允许范围内。")
    if t in ("number", "currency"):
        try:
            return float(str(value).replace(",", "").replace("¥", "").replace(" ", ""))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"字段 {field} 需要是数字，收到 '{value}'。")
    if t == "date":
        from datetime import date as _date, datetime as _datetime
        if isinstance(value, _datetime):
            return value.date().isoformat()
        if isinstance(value, _date):
            return value.isoformat()
        s = str(value).strip().replace("/", "-").split("T")[0].split(" ")[0]
        try:
            return _date.fromisoformat(s).isoformat()
        except ValueError:
            raise HTTPException(status_code=400, detail=f"字段 {field} 需要是日期（YYYY-MM-DD），收到 '{value}'。")
    return str(value)


def _apply_update(row, entity: str, values: dict[str, Any]):
    """Whitelist + coerce + assign incoming values onto the row. Returns count applied."""
    schema = {c["name"]: c for c in (COLUMN_META.get(entity) or []) if not c.get("virtual")}
    # Reject clearing NOT NULL columns with a friendly 400 instead of a DB 500
    not_null = {c.name for c in row.__table__.columns if not c.nullable and not c.primary_key
                and c.server_default is None}
    applied = 0
    for field, value in values.items():
        if field in NON_EDITABLE_COLUMNS:
            continue
        if field not in schema:
            continue  # silently ignore unknown fields — frontend renders from schema
        col_meta = schema[field]
        coerced = _coerce_value(entity, field, value, col_meta)
        if coerced is None and field in not_null:
            raise HTTPException(status_code=400, detail=f"字段 {col_meta.get('label', field)} 不能为空。")

        # Enum columns on models are Python Enum classes
        if col_meta["type"] == "enum" and coerced is not None:
            from ... import models as M
            enum_cls = {
                "spaces": {"status": M.SpaceStatus, "space_type": M.SpaceType},
                "tenants": {"status": M.TenantStatus, "tenant_type": M.TenantType},
                "leases": {"status": M.LeaseStatus, "payment_cycle": M.PaymentCycle},
                "charges": {"status": M.ChargeStatus, "charge_type": M.ChargeType},
                "payments": {"status": M.PaymentStatus, "payment_method": M.PaymentMethod},
            }.get(entity, {})
            cls = enum_cls.get(field)
            if cls is not None:
                coerced = cls(coerced)
        setattr(row, field, coerced)
        applied += 1
    return applied


@router.patch("/{table_ref}/records/{record_id}")
def update_record(
    table_ref: str,
    record_id: str,
    body: RecordUpdateRequest,
    project_id: str = Query(...),
    db: Session = Depends(get_db),
    user: AppUser = Depends(CurrentUser()),
):
    if not require_project_scope(project_id, user, db):
        raise HTTPException(status_code=403, detail="无权访问此项目。")
    ensure_project_in_active_org(uuid.UUID(project_id), user, db)
    if not table_ref.startswith("business:"):
        raise HTTPException(status_code=400, detail="导入原始数据为只读快照，不能修改。请编辑对应的业务数据。")
    entity = table_ref.split(":", 1)[1]
    ModelClass = _business_model(entity)

    try:
        rid = uuid.UUID(record_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的记录 ID。")

    row = db.query(ModelClass).filter(ModelClass.id == rid, ModelClass.project_id == uuid.UUID(project_id)).first()
    if not row:
        raise HTTPException(status_code=404, detail="记录不存在。")

    applied = _apply_update(row, entity, body.values)
    if applied == 0:
        raise HTTPException(status_code=400, detail="没有可更新的字段。")
    if hasattr(row, "version"):
        row.version = (row.version or 1) + 1

    db.add(AuditLog(
        actor_user_id=user.id,
        action="table_record_update",
        resource_type=entity,
        resource_id=str(rid),
        project_id=uuid.UUID(project_id),
    ))
    db.commit()
    db.refresh(row)
    return {"ok": True, "applied": applied, "id": str(rid)}


# ---------- Create record (business tables only) ----------

class RecordCreateRequest(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


def _create_business_row(db: Session, entity: str, values: dict[str, Any], project_id: uuid.UUID, org_id: uuid.UUID):
    """Build a new ORM instance from the incoming values dict."""
    schema = {c["name"]: c for c in (COLUMN_META.get(entity) or []) if not c.get("virtual")}
    ModelClass = _business_model(entity)

    kwargs: dict[str, Any] = {"project_id": project_id, "organization_id": org_id}
    for field, value in values.items():
        if field in NON_EDITABLE_COLUMNS:
            continue
        if field not in schema:
            continue
        col_meta = schema[field]
        coerced = _coerce_value(entity, field, value, col_meta)
        if col_meta["type"] == "enum" and coerced is not None:
            enum_cls = {
                "spaces": {"status": M.SpaceStatus, "space_type": M.SpaceType},
                "tenants": {"status": M.TenantStatus, "tenant_type": M.TenantType},
                "leases": {"status": M.LeaseStatus, "payment_cycle": M.PaymentCycle},
                "charges": {"status": M.ChargeStatus, "charge_type": M.ChargeType},
                "payments": {"status": M.PaymentStatus, "payment_method": M.PaymentMethod},
            }.get(entity, {})
            cls = enum_cls.get(field)
            if cls is not None:
                coerced = cls(coerced)
        kwargs[field] = coerced

    # Required fields — tenant_name for tenant, lease_code for lease, etc.
    required = {
        "spaces": ["space_code"],
        "tenants": ["tenant_name"],
        "leases": ["lease_code", "tenant_id", "space_id", "start_date", "end_date", "rent_amount"],
        "charges": ["amount_due"],
        "payments": ["amount"],
    }.get(entity, [])
    missing = [f for f in required if kwargs.get(f) is None]
    if missing:
        labels = {schema[f]["name"]: schema[f]["label"] for f in missing if f in schema}
        raise HTTPException(status_code=400, detail=f"缺少必填字段：{', '.join(labels.values() or missing)}")

    # For leases, tenant_id and space_id are foreign keys — accept tenant_name/space_code lookup
    if entity == "leases":
        tid = kwargs.pop("tenant_id", None)
        sid = kwargs.pop("space_id", None)
        tname = kwargs.pop("tenant_name", None)
        scode = kwargs.pop("space_name", None)

        if not tid and tname:
            t = db.query(M.Tenant).filter(M.Tenant.tenant_name == tname, M.Tenant.project_id == project_id).first()
            if not t:
                raise HTTPException(status_code=400, detail=f"租户 '{tname}' 不存在。")
            tid = t.id
        if not tid:
            raise HTTPException(status_code=400, detail="请指定租户（tenant_name 或 tenant_id）。")

        if not sid and scode:
            s = db.query(M.Space).filter(M.Space.space_code == scode, M.Space.project_id == project_id).first()
            if not s:
                raise HTTPException(status_code=400, detail=f"空间 '{scode}' 不存在。")
            sid = s.id
        if not sid:
            raise HTTPException(status_code=400, detail="请指定空间（space_name 或 space_id）。")

        kwargs["tenant_id"] = tid
        kwargs["space_id"] = sid

    return ModelClass(**kwargs)


@router.post("/{table_ref}/records")
def create_record(
    table_ref: str,
    body: RecordCreateRequest,
    project_id: str = Query(...),
    db: Session = Depends(get_db),
    user: AppUser = Depends(CurrentUser()),
):
    """Create a new record in a business table."""
    if not require_project_scope(project_id, user, db):
        raise HTTPException(status_code=403, detail="无权访问此项目。")
    if not table_ref.startswith("business:"):
        raise HTTPException(status_code=400, detail="只能在业务表中新建记录。")
    entity = table_ref.split(":", 1)[1]
    pid = uuid.UUID(project_id)
    org_id = ensure_project_in_active_org(pid, user, db)

    proj = db.query(Project).filter(Project.id == pid).first()
    if not proj:
        raise HTTPException(status_code=404, detail="项目不存在。")

    row = _create_business_row(db, entity, body.values, pid, org_id)
    db.add(row)
    db.flush()  # get row.id for audit log
    db.add(AuditLog(
        actor_user_id=user.id,
        action="table_record_create",
        resource_type=entity,
        resource_id=str(row.id),
        project_id=pid,
    ))
    db.commit()
    db.refresh(row)
    return {"ok": True, "id": str(row.id)}


@router.get("/{table_ref}/aggregate")
def get_aggregate(
    table_ref: str,
    project_id: str = Query(...),
    search: Optional[str] = None,
    filters: Optional[str] = None,
    db: Session = Depends(get_db),
    user: AppUser = Depends(CurrentUser()),
):
    """Return SUM/AVG/COUNT/MIN/MAX for all numeric/currency columns."""
    if not require_project_scope(project_id, user, db):
        raise HTTPException(status_code=403, detail="无权访问此项目。")
    ensure_project_in_active_org(uuid.UUID(project_id), user, db)
    if not table_ref.startswith("business:"):
        raise HTTPException(status_code=400, detail="聚合仅支持业务表。")

    entity = table_ref.split(":", 1)[1]
    ModelClass = _business_model(entity)
    pid = uuid.UUID(project_id)

    from sqlalchemy import func, String as SAString, Enum as SAEnum

    q = db.query(ModelClass).filter(ModelClass.project_id == pid)

    if search:
        string_cols = [c.name for c in ModelClass.__table__.columns
                       if isinstance(c.type, SAString) and not isinstance(c.type, SAEnum)]
        if string_cols:
            q = q.filter(or_(*[getattr(ModelClass, c).ilike(f"%{search}%") for c in string_cols]))

    col_names = {c.name for c in ModelClass.__table__.columns}
    for field, op, value in _parse_filters(filters):
        if field not in col_names:
            continue
        col = getattr(ModelClass, field)
        if op == "eq":
            q = q.filter(col == value)
        elif op == "ne":
            q = q.filter(col != value)
        elif op == "gt":
            q = q.filter(col > _safe_num(value))
        elif op == "gte":
            q = q.filter(col >= _safe_num(value))
        elif op == "lt":
            q = q.filter(col < _safe_num(value))
        elif op == "lte":
            q = q.filter(col <= _safe_num(value))
        elif op == "like":
            q = q.filter(col.ilike(f"%{value}%"))
        elif op == "in":
            q = q.filter(col.in_([v.strip() for v in value.split("|")]))
        elif op == "between":
            a, b = value.split("|", 1)
            num_types = (Numeric, Integer, Float, SmallInteger, BigInteger)
            if isinstance(col.type, num_types):
                a_v, b_v = _safe_num(a), _safe_num(b)
            elif isinstance(col.type, Date):
                from datetime import date as _date
                a_v, b_v = _date.fromisoformat(a), _date.fromisoformat(b)
            elif isinstance(col.type, DateTimeType):
                from datetime import datetime as _dt
                a_v = _dt.fromisoformat(a) if "T" in a or " " in a else _dt.fromisoformat(a + "T00:00:00")
                b_v = _dt.fromisoformat(b) if "T" in b or " " in b else _dt.fromisoformat(b + "T23:59:59")
            else:
                a_v, b_v = a, b
            q = q.filter(col >= a_v, col <= b_v)

    # Find numeric/currency columns from COLUMN_META
    numeric_cols = [c["name"] for c in (COLUMN_META.get(entity) or [])
                    if c["type"] in ("number", "currency") and not c.get("virtual")]

    result: dict[str, dict[str, float | int | None]] = {}
    for cname in numeric_cols:
        if cname not in col_names:
            continue
        col = getattr(ModelClass, cname)
        agg_q = q.with_entities(
            func.sum(col), func.avg(col), func.count(col), func.min(col), func.max(col)
        )
        row = agg_q.first()
        def _f(x):
            if x is None:
                return None
            if isinstance(x, Decimal):
                return float(x)
            return float(x)
        result[cname] = {
            "sum": _f(row[0]),
            "avg": _f(row[1]),
            "count": row[2],
            "min": _f(row[3]),
            "max": _f(row[4]),
        }

    total = q.count()
    return {"total": total, "columns": result}


@router.delete("/{table_ref}/records/{record_id}")
def delete_record(
    table_ref: str,
    record_id: str,
    project_id: str = Query(...),
    db: Session = Depends(get_db),
    user: AppUser = Depends(CurrentUser()),
):
    if not require_project_scope(project_id, user, db):
        raise HTTPException(status_code=403, detail="无权访问此项目。")
    ensure_project_in_active_org(uuid.UUID(project_id), user, db)
    if not table_ref.startswith("business:"):
        raise HTTPException(status_code=400, detail="导入原始数据为只读快照，不能删除。")
    entity = table_ref.split(":", 1)[1]
    ModelClass = _business_model(entity)

    try:
        rid = uuid.UUID(record_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的记录 ID。")

    row = db.query(ModelClass).filter(ModelClass.id == rid, ModelClass.project_id == uuid.UUID(project_id)).first()
    if not row:
        raise HTTPException(status_code=404, detail="记录不存在。")

    db.add(AuditLog(
        actor_user_id=user.id,
        action="table_record_delete",
        resource_type=entity,
        resource_id=str(rid),
        project_id=uuid.UUID(project_id),
    ))
    db.delete(row)
    db.commit()
    return {"ok": True, "id": str(rid)}


# ============================================================
#  APITable 云版同步端点
# ============================================================

class SyncApitableRequest(BaseModel):
    entity: Optional[str] = Field(None, description="要同步的业务实体；不传则同步全部 5 个")


BUSINESS_ENTITIES = ("spaces", "tenants", "leases", "charges", "payments")


@router.get("/sync-apitable/status")
def apitable_status(
    db: Session = Depends(get_db),
    user: AppUser = Depends(CurrentUser()),
):
    """返回 APITable 集成配置状态（不含敏感 Token 明文）。"""
    settings = get_settings()
    return {
        "configured": settings.apitable_configured,
        "base_url": settings.apitable_base_url,
        "token_set": bool(settings.apitable_token),
        "space_id_set": bool(settings.apitable_space_id),
    }


@router.post("/sync-apitable")
async def sync_to_apitable(
    req: Optional[SyncApitableRequest],
    project_id: str = Query(...),
    db: Session = Depends(get_db),
    user: AppUser = Depends(CurrentUser()),
):
    """将本项目的业务数据全量同步到 APITable 云版对应 datasheet。

    这是单向同步（neon → APITable），每次同步会清空 APITable 现有记录后重写。
    """
    if not require_project_scope(project_id, user, db):
        raise HTTPException(status_code=403, detail="无权访问此项目。")
    ensure_project_in_active_org(uuid.UUID(project_id), user, db)

    settings = get_settings()
    if not settings.apitable_configured:
        raise HTTPException(
            status_code=503,
            detail={
                "message": "APITable 尚未配置",
                "env_vars": ["APITABLE_TOKEN", "APITABLE_SPACE_ID"],
                "how_to": "在 Railway 项目 Settings → Variables 中添加这两个环境变量后重新部署",
            },
        )

    entity = req.entity if req else None
    entities = [entity] if entity else list(BUSINESS_ENTITIES)
    if any(e not in BUSINESS_ENTITIES for e in entities):
        raise HTTPException(status_code=400, detail=f"未知实体: {entity}")

    results: list[dict] = []
    errors: list[dict] = []

    for ent in entities:
        try:
            # 1. 从 Neon 读全部数据
            ModelClass = _business_model(ent)
            q = db.query(ModelClass).filter(ModelClass.project_id == uuid.UUID(project_id))
            db_rows = q.all()
            records = [row_to_dict(r) for r in db_rows]
            col_meta = COLUMN_META.get(ent, [])

            # 2. 异步同步到 APITable
            result = await sync_entity(ent, records, col_meta)
            results.append(result)

            # 3. 记录审计日志
            db.add(AuditLog(
                actor_user_id=user.id,
                action="apitable_sync",
                resource_type=ent,
                resource_id="",
                project_id=uuid.UUID(project_id),
                after_summary={"written": result.get("written", 0), "datasheet": result.get("datasheet_name")},
            ))
        except APITableError as e:
            errors.append({"entity": ent, "error": str(e), "code": e.code})
        except Exception as e:
            logger.exception("同步 %s 到 APITable 失败", ent)
            errors.append({"entity": ent, "error": str(e)})

    db.commit()

    return {
        "ok": len(errors) == 0,
        "results": results,
        "errors": errors,
        "total_entities": len(entities),
        "synced_entities": len(results),
    }


def row_to_dict(row) -> dict[str, Any]:
    """把 SQLAlchemy ORM 对象序列化为 dict（跳过关系属性）。"""
    from sqlalchemy.orm import ColumnProperty
    result: dict[str, Any] = {}
    for attr in row.__mapper__.attrs:
        if isinstance(attr, ColumnProperty):
            result[attr.key] = getattr(row, attr.key)
    return result
