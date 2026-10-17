"""Excel/CSV import pipeline — upload, parse, field inference, mapping, preview, confirm.

Pipeline matches spec §6:
  uploaded -> scanning -> parsing -> mapping_required -> preview_ready -> importing -> completed
  失败: rejected / failed / cancelled
"""
from __future__ import annotations

import csv
import io
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import openpyxl
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..models import (
    FieldMapping,
    ImportJob,
    ImportJobStatus,
    ImportRow,
    ImportRowValidationStatus,
    ImportSheet,
)
from . import storage

logger = logging.getLogger(__name__)

settings = get_settings()

ALLOWED_EXT = {".xlsx", ".csv"}
MAX_CSV_SAMPLE_BYTES = 65536


# ---------- Data classes ----------

@dataclass
class ParsedSheet:
    sheet_name: str
    header_row_index: int
    headers: list[str]
    rows: list[list[Any]]  # each row: list of cell values (str/int/float/bool/None)
    raw_json: list[dict]  # row dicts keyed by header


@dataclass
class ParseResult:
    sheets: list[ParsedSheet] = field(default_factory=list)
    total_rows: int = 0


# ---------- Security checks ----------

def validate_file(filename: str, file_size: int) -> tuple[bool, str]:
    """Check extension + size. Returns (ok, reason)."""
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        return False, f"不支持的文件类型：{ext}（允许 {', '.join(sorted(ALLOWED_EXT))}）"
    max_bytes = settings.max_upload_mb * 1024 * 1024
    if file_size > max_bytes:
        return False, f"文件过大（{file_size} 字节），最大允许 {settings.max_upload_mb} MB。"
    return True, "ok"


# ---------- Parsers ----------

def parse_xlsx(content: bytes) -> ParseResult:
    """Parse .xlsx into sheets with header + raw rows."""
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    result = ParseResult()

    for ws in wb.worksheets:
        if ws.max_row is None or ws.max_row == 0:
            continue
        if len(result.sheets) >= settings.max_import_sheets:
            break

        # Read all rows as Python values
        raw_rows: list[list[Any]] = []
        for row in ws.iter_rows(values_only=True):
            raw_rows.append(list(row))
            if len(raw_rows) > settings.max_import_rows:
                break

        if not raw_rows:
            continue

        # Smart header-row detection: workbooks often start with an all-empty row
        # (merged banner cell) and/or a title line like "租户信息（19 条测试数据）".
        # Scan the first few rows and take the first row that looks like a real
        # header: most cells non-empty.
        header_idx = 0
        for i, candidate in enumerate(raw_rows[:6]):
            non_empty = [c for c in candidate if c is not None and str(c).strip()]
            if len(non_empty) >= max(2, len(candidate) // 2):
                header_idx = i
                break
        headers = [str(c) if c is not None else f"col_{i}" for i, c in enumerate(raw_rows[header_idx])]
        # Ensure unique headers
        seen: set[str] = set()
        for i, h in enumerate(headers):
            if h in seen:
                headers[i] = f"{h}_{i}"
            seen.add(headers[i])

        data_rows = [r for r in raw_rows[header_idx + 1 :] if any(c is not None and str(c).strip() for c in r)]
        raw_json = []
        for r in data_rows:
            # pad or truncate to header length
            padded = list(r) + [None] * (len(headers) - len(r))
            raw_json.append({headers[i]: padded[i] for i in range(len(headers))})

        sheet = ParsedSheet(
            sheet_name=ws.title,
            header_row_index=header_idx,
            headers=headers,
            rows=data_rows,
            raw_json=raw_json,
        )
        result.sheets.append(sheet)
        result.total_rows += len(data_rows)

    wb.close()
    return result


def parse_csv(content: bytes, filename: str) -> ParseResult:
    """Parse a single CSV file into one sheet. Try utf-8 → gbk fallback."""
    text = None
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            text = content.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = content.decode("utf-8", errors="replace")

    # Detect delimiter with csv.Sniffer
    sample = text[:MAX_CSV_SAMPLE_BYTES]
    delimiter = ","
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        pass

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows: list[list[Any]] = []
    for row in reader:
        rows.append([_coerce_csv_cell(c) for c in row])
        if len(rows) > settings.max_import_rows + 1:  # header + data
            break

    if not rows:
        return ParseResult()

    headers = [str(c) if c is not None else f"col_{i}" for i, c in enumerate(rows[0])]
    seen: set[str] = set()
    for i, h in enumerate(headers):
        if h in seen:
            headers[i] = f"{h}_{i}"
        seen.add(headers[i])

    data_rows = rows[1:]
    raw_json = [{headers[i]: (list(r) + [None] * (len(headers) - len(r)))[i] for i in range(len(headers))} for r in data_rows]

    return ParseResult(
        sheets=[
            ParsedSheet(
                sheet_name=Path(filename).stem,
                header_row_index=0,
                headers=headers,
                rows=data_rows,
                raw_json=raw_json,
            )
        ],
        total_rows=len(data_rows),
    )


def _coerce_csv_cell(c: str) -> Any:
    """Safe CSV cell coercion. Numbers only if unambiguously numeric."""
    if c is None:
        return None
    stripped = c.strip()
    if stripped == "":
        return None
    if stripped.lower() in ("true", "false"):
        return stripped.lower() == "true"
    # Don't auto-coerce CSV cells to numbers — they could be IDs with leading zeros
    return c


def parse_any(content: bytes, filename: str) -> ParseResult:
    ext = Path(filename).suffix.lower()
    if ext == ".csv":
        return parse_csv(content, filename)
    if ext == ".xlsx":
        return parse_xlsx(content)
    raise ValueError(f"Unsupported extension: {ext}")


# ---------- Field type inference ----------

def infer_field_type(header: str, sample_values: list[Any]) -> str:
    """Simple rule-based type inference: text | integer | decimal | date | amount | boolean."""
    non_null = [v for v in sample_values if v is not None and v != ""]
    if not non_null:
        return "text"

    h = header.lower()

    # Booleans
    if all(isinstance(v, bool) for v in non_null):
        return "boolean"

    # Date-ish
    date_keywords = ("日期", "时间", "date", "time", "签约", "start", "end", "到期")
    if any(k in h for k in date_keywords):
        # Try parsing
        try:
            from datetime import date
            for v in non_null[:5]:
                if isinstance(v, (datetime, date)):
                    continue
                if isinstance(v, str):
                    date.fromisoformat(v.replace("/", "-"))
            return "date"
        except Exception:
            pass

    # Enum-ish (classification fields like "费用类型" should NOT be amount)
    enum_keywords = ("类型", "分类", "类别", "类型", "type", "category", "class")
    if any(k in h for k in enum_keywords):
        return "text"

    # Amount-ish
    amount_keywords = ("金额", "租金", "押金", "费用", "收款", "amount", "fee", "rent", "deposit", "due", "paid")
    if any(k in h for k in amount_keywords):
        return "amount"

    # Numbers
    all_int = True
    all_num = True
    for v in non_null:
        if isinstance(v, bool):
            all_num = False
            all_int = False
            continue
        if isinstance(v, (int, float)):
            if isinstance(v, float) and v != int(v):
                all_int = False
        elif isinstance(v, str):
            try:
                if "." in v:
                    float(v.replace(",", ""))
                    all_int = False
                else:
                    int(v.replace(",", ""))
            except ValueError:
                all_num = False
                all_int = False
                break
    if all_num:
        return "integer" if all_int else "decimal"

    return "text"


# ---------- Rule-based mapping suggestions ----------

# Standard field keywords → target (entity, field)
STANDARD_FIELD_KEYWORDS: list[tuple[set[str], str, str]] = [
    # Spaces
    ({"空间编号", "房间号", "room", "space_code", "space_code"}, "space", "space_code"),
    ({"楼层", "floor"}, "space", "floor"),
    ({"面积", "area", "面积(㎡)", "面积m2"}, "space", "area_sqm"),
    ({"工位", "workstation", "workstation_count"}, "space", "workstation_count"),
    ({"楼宇", "building"}, "space", "building_id"),
    # Tenants — 更具体的关键字排在前面，避免"租户类型"被"租户"误匹配
    ({"租户类型", "tenant_type", "企业类型"}, "tenant", "tenant_type"),
    ({"租户", "tenant", "tenant_name", "公司名"}, "tenant", "tenant_name"),
    ({"联系人", "contact"}, "tenant", "contact_name"),
    ({"电话", "phone", "mobile"}, "tenant", "contact_phone"),
    ({"行业", "industry"}, "tenant", "industry"),
    ({"统一社会信用代码", "信用代码", "registration_code", "税号"}, "tenant", "registration_code"),
    # Leases
    ({"合同编号", "lease_code", "合同号", "合同"}, "lease", "lease_code"),
    ({"开始日期", "起租日期", "起租", "start_date", "start"}, "lease", "start_date"),
    ({"结束日期", "end_date", "expiry", "到期"}, "lease", "end_date"),
    ({"签约日期", "signed_date", "signed"}, "lease", "contract_signed_date"),
    ({"租金标准", "租金", "rent", "rent_amount"}, "lease", "rent_amount"),
    ({"押金", "deposit", "deposit_amount"}, "lease", "deposit_amount"),
    ({"付款周期", "缴费周期", "payment_cycle", "支付周期"}, "lease", "payment_cycle"),
    # Charges / Payments ingest 暂未实现，不自动映射，避免数据静默丢弃
]


def suggest_mapping(headers: list[str]) -> list[dict[str, Any]]:
    """Rule-based mapping suggestions with confidence."""
    mappings: list[dict[str, Any]] = []
    for h in headers:
        hl = h.lower()
        suggestion = {
            "source_field_name": h,
            "target_field_name": None,
            "target_entity": None,
            "target_type": "text",
            "confidence": None,
            "mapping_method": "rule_based",
        }
        for keywords, entity, target in STANDARD_FIELD_KEYWORDS:
            for kw in keywords:
                if kw in hl or kw in h:
                    suggestion["target_field_name"] = target
                    suggestion["target_entity"] = entity
                    suggestion["confidence"] = Decimal("0.80")
                    break
            if suggestion["target_field_name"]:
                break
        mappings.append(suggestion)
    return mappings


# ---------- LLM-augmented mapping ----------
#
# Allowed target fields per entity — LLM 必须从这些选项中选，禁止生成任意值。
ALLOWED_TARGETS: dict[str, list[str]] = {
    "space": ["space_code", "floor", "area_sqm", "workstation_count", "building_name"],
    "tenant": ["tenant_name", "tenant_type", "contact_name", "contact_phone", "industry", "registration_code"],
    "lease": ["lease_code", "start_date", "end_date", "contract_signed_date", "rent_amount",
              "deposit_amount", "payment_cycle", "status"],
}

ALLOWED_TYPES = {"text", "integer", "decimal", "amount", "date", "boolean"}


def _build_mapping_prompt(headers: list[str], samples: dict[str, list[str]]) -> str:
    """Build a strict prompt for LLM to suggest field mappings.

    - 列名 + 前 3 个非空样本值
    - LLM 必须从允许的目标字段中选择，禁止编造
    - 返回 JSON 数组，每项 {source_field_name, target_field_name, target_entity, target_type, confidence, reasoning}
    """
    options_lines = []
    for ent, fields in ALLOWED_TARGETS.items():
        options_lines.append(f"- {ent}: {', '.join(fields)}")
    options_block = "\n".join(options_lines)

    header_lines = []
    for h in headers:
        s = [v for v in samples.get(h, []) if v is not None and v != ""][:3]
        sample_str = " | ".join(str(v) for v in s) if s else "（空）"
        header_lines.append(f"- \"{h}\"  样本: {sample_str}")

    return f"""你是写字楼空间租赁管理系统的字段映射助手。根据 Excel 表头和样本数据，建议每个源字段应映射到哪个目标字段。

【业务实体与允许的目标字段】（严格在此范围内选，禁止编造新字段）
{options_block}

【允许的数据类型】text / integer / decimal / amount / date / boolean

【源字段 + 样本】
{chr(10).join(header_lines)}

【输出要求】
仅返回 JSON 数组，不要任何解释、不要 Markdown 代码块。格式：
[
  {{"source_field_name":"...", "target_field_name":"..."或 null, "target_entity":"..."或 null, "target_type":"text|integer|decimal|amount|date|boolean", "confidence": 0.0~1.0, "reasoning":"一句话理由"}}
]

如果某个源字段无法判断目标，target_field_name 和 target_entity 设为 null。
confidence < 0.7 的映射建议用户在 UI 中确认。
"""


def suggest_mapping_llm(
    headers: list[str],
    samples: dict[str, list[str]],
) -> list[dict[str, Any]]:
    """LLM-augmented mapping suggestions.

    调用 LLM 推断所有字段；对未匹配或低置信度的字段，再叠加规则兜底。
    LLM 不可用时完全降级到 suggest_mapping()。
    """
    from .llm_provider import chat, LLMError, is_configured

    if not is_configured():
        # 完全降级：使用原规则实现
        return suggest_mapping(headers)

    prompt = _build_mapping_prompt(headers, samples)

    try:
        raw = chat(
            [{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=2048,
        )
    except LLMError as e:
        logger.warning("[import] LLM 不可用 (%s)，降级到规则建议", e.reason)
        return suggest_mapping(headers)

    # 解析 LLM 返回的 JSON
    import json as _json
    import re as _re

    # 提取 JSON 数组（容忍模型偶尔输出 ```json ``` 包裹）
    match = _re.search(r"\[.*\]", raw, _re.DOTALL)
    if not match:
        logger.warning("[import] LLM 返回无法解析 (%s)，降级到规则建议", raw[:200])
        return suggest_mapping(headers)
    try:
        llm_mappings = _json.loads(match.group(0))
    except _json.JSONDecodeError:
        logger.warning("[import] LLM JSON 解析失败，降级到规则建议")
        return suggest_mapping(headers)

    if not isinstance(llm_mappings, list):
        return suggest_mapping(headers)

    # 校验：过滤非法 target_field / target_entity / target_type
    valid: list[dict[str, Any]] = []
    for item in llm_mappings:
        if not isinstance(item, dict):
            continue
        src = item.get("source_field_name")
        if not src or src not in headers:
            continue
        ent = item.get("target_entity")
        fld = item.get("target_field_name")
        typ = item.get("target_type", "text")
        if typ not in ALLOWED_TYPES:
            typ = "text"
        if ent is not None and ent not in ALLOWED_TARGETS:
            ent = None
            fld = None
        if ent is not None and fld is not None and fld not in ALLOWED_TARGETS[ent]:
            fld = None
            ent = None
        try:
            conf = float(item.get("confidence", 0.0))
            conf = max(0.0, min(1.0, conf))
        except (TypeError, ValueError):
            conf = 0.0
        valid.append({
            "source_field_name": src,
            "target_field_name": fld,
            "target_entity": ent,
            "target_type": typ,
            "confidence": Decimal(str(round(conf, 2))),
            "mapping_method": "ai_suggested",
            "reasoning": (item.get("reasoning") or "")[:200],
        })

    # 对 LLM 未覆盖的源字段（漏掉或被过滤），用规则兜底
    llm_covered = {m["source_field_name"] for m in valid}
    rule_fallback = suggest_mapping([h for h in headers if h not in llm_covered])
    rule_fallback = [{**m, "mapping_method": "rule_based"} for m in rule_fallback]

    # 按 headers 顺序合并
    by_src = {m["source_field_name"]: m for m in valid}
    for m in rule_fallback:
        by_src[m["source_field_name"]] = m
    return [by_src[h] for h in headers if h in by_src]


def extract_samples(
    headers: list[str],
    rows: list[dict[str, Any]],
    *,
    max_samples: int = 5,
) -> dict[str, list[str]]:
    """从 ImportRow.raw_values 提取每个表头的前 N 个非空样本值。"""
    out: dict[str, list[str]] = {h: [] for h in headers}
    for row in rows:
        for h in headers:
            if len(out[h]) >= max_samples:
                continue
            v = row.get(h)
            if v is None or v == "":
                continue
            sv = v.isoformat() if hasattr(v, "isoformat") else str(v)
            out[h].append(sv)
        if all(len(out[h]) >= max_samples for h in headers):
            break
    return out


# ---------- Validation ----------

def validate_row(raw_values: dict[str, Any], mapping: list[FieldMapping]) -> dict[str, Any]:
    """Run deterministic validations. Returns {status, errors: [...], warnings: [...]}."""
    errors: list[str] = []
    warnings: list[str] = []
    normalized: dict[str, Any] = {}

    for m in mapping:
        val = raw_values.get(m.source_field_name)
        if val is None or val == "":
            if m.target_field_name and m.target_entity == "space" and m.target_field_name == "space_code":
                errors.append("空间编号为空")
            continue
        try:
            ntype = m.target_type or "text"
            if ntype in ("integer", "decimal", "amount"):
                s = str(val).replace(",", "").replace("¥", "").replace(" ", "")
                dec = Decimal(s)
                if ntype == "integer":
                    normalized[m.source_field_name] = int(dec)
                else:
                    normalized[m.source_field_name] = float(dec)
                if dec < 0 and m.target_field_name in ("rent_amount", "deposit_amount", "amount_due"):
                    warnings.append(f"{m.source_field_name} 金额为负数：{val}")
            elif ntype == "date":
                from datetime import date
                if isinstance(val, datetime):
                    normalized[m.source_field_name] = val.date().isoformat()
                elif isinstance(val, date):
                    normalized[m.source_field_name] = val.isoformat()
                elif isinstance(val, str):
                    normalized[m.source_field_name] = date.fromisoformat(val.replace("/", "-")).isoformat()
            elif ntype == "boolean":
                normalized[m.source_field_name] = bool(val)
            else:
                normalized[m.source_field_name] = str(val)
        except (InvalidOperation, ValueError) as exc:
            errors.append(f"字段 '{m.source_field_name}' 值 '{val}' 不符合类型 {ntype}: {exc}")
            normalized[m.source_field_name] = val

    status = ImportRowValidationStatus.error if errors else (ImportRowValidationStatus.warning if warnings else ImportRowValidationStatus.valid)
    return {
        "status": status,
        "errors": errors,
        "warnings": warnings,
        "normalized_values": normalized,
    }


# ---------- Service orchestration ----------

def start_import_job(
    db: Session,
    *,
    project_id: uuid.UUID,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    filename: str,
    content: bytes,
) -> ImportJob:
    """Phase 1: upload + save + parse. Creates ImportJob + ImportSheets + placeholder rows."""
    ok, reason = validate_file(filename, len(content))
    if not ok:
        raise ValueError(reason)

    # Save
    storage_key, sha256 = storage.save_uploaded_file(content, filename)

    # Parse
    try:
        parse_result = parse_any(content, filename)
    except Exception as exc:
        raise ValueError(f"文件解析失败: {exc}")

    if not parse_result.sheets:
        raise ValueError("工作簿为空或无可解析内容。")

    job = ImportJob(
        id=uuid.uuid4(),
        organization_id=org_id,
        project_id=project_id,
        uploaded_by=user_id,
        original_filename=filename,
        storage_key=storage_key,
        sha256=sha256,
        file_size=len(content),
        status=ImportJobStatus.parsing,
        sheet_count=len(parse_result.sheets),
        row_count=parse_result.total_rows,
    )
    db.add(job)
    db.flush()

    for parsed_sheet in parse_result.sheets:
        sheet = ImportSheet(
            id=uuid.uuid4(),
            import_job_id=job.id,
            sheet_name=parsed_sheet.sheet_name,
            header_row_index=parsed_sheet.header_row_index,
            row_count=len(parsed_sheet.rows),
            column_count=len(parsed_sheet.headers),
            status="parsed",
        )
        db.add(sheet)
        db.flush()

        # Create placeholder rows (raw values only, validation_pending)
        for idx, row_dict in enumerate(parsed_sheet.raw_json):
            raw_values_json = json.dumps(row_dict, default=str, ensure_ascii=False)
            db.add(
                ImportRow(
                    id=uuid.uuid4(),
                    organization_id=org_id,
                    project_id=project_id,
                    import_job_id=job.id,
                    import_sheet_id=sheet.id,
                    source_row_number=idx + 1,
                    raw_values=json.loads(raw_values_json),
                    validation_status=ImportRowValidationStatus.pending,
                )
            )

        # Field mapping suggestions
        suggestions = suggest_mapping(parsed_sheet.headers)
        for s in suggestions:
            conf = s.get("confidence")
            db.add(
                FieldMapping(
                    id=uuid.uuid4(),
                    organization_id=org_id,
                    project_id=project_id,
                    import_sheet_id=sheet.id,
                    source_field_name=s["source_field_name"],
                    target_field_name=s["target_field_name"],
                    target_entity=s["target_entity"],
                    target_type=infer_field_type(s["source_field_name"], [row_dict.get(s["source_field_name"]) for row_dict in parsed_sheet.raw_json[:20]]),
                    mapping_method=s["mapping_method"],
                    confidence=conf,
                )
            )

    job.status = ImportJobStatus.mapping_required
    db.commit()
    db.refresh(job)
    return job


def run_preview(db: Session, job_id: uuid.UUID) -> dict[str, Any]:
    """Run validation on all rows, return preview stats."""
    job = db.query(ImportJob).filter(ImportJob.id == job_id).first()
    if not job:
        raise ValueError("Import job not found")

    sheets = db.query(ImportSheet).filter(ImportSheet.import_job_id == job.id).all()
    mappings_all = db.query(FieldMapping).filter(FieldMapping.project_id == job.project_id).all()

    total = 0
    valid = 0
    error = 0
    warning = 0
    sample_errors: list[dict[str, Any]] = []

    for sheet in sheets:
        mappings = [m for m in mappings_all if m.import_sheet_id == sheet.id]
        rows = db.query(ImportRow).filter(ImportRow.import_sheet_id == sheet.id).all()
        for row in rows:
            total += 1
            result = validate_row(row.raw_values, mappings)
            row.validation_status = result["status"]
            row.normalized_values = result["normalized_values"]
            if result["status"] == ImportRowValidationStatus.error:
                error += 1
                if len(sample_errors) < 20:
                    sample_errors.append({
                        "sheet": sheet.sheet_name,
                        "row": row.source_row_number,
                        "errors": result["errors"],
                        "raw": row.raw_values,
                    })
            elif result["status"] == ImportRowValidationStatus.warning:
                warning += 1
            else:
                valid += 1

    job.status = ImportJobStatus.preview_ready
    job.error_count = error
    job.success_count = valid
    db.commit()

    return {
        "job_id": str(job.id),
        "total": total,
        "valid": valid,
        "warning": warning,
        "error": error,
        "sample_errors": sample_errors,
        "status": job.status.value if hasattr(job.status, "value") else str(job.status),
    }


def confirm_import(db: Session, job_id: uuid.UUID) -> ImportJob:
    """Confirm an import job: write valid rows into business tables + mark completed."""
    job = db.query(ImportJob).filter(ImportJob.id == job_id).first()
    if not job:
        raise ValueError("Import job not found")
    if job.status not in (ImportJobStatus.preview_ready, ImportJobStatus.mapping_required):
        raise ValueError(f"Import job is not confirmable (current={job.status.value})")

    job.status = ImportJobStatus.importing
    db.commit()
    try:
        ingest_business_tables(db, job)
    except Exception:
        job.status = ImportJobStatus.preview_ready
        db.commit()
        raise

    job.status = ImportJobStatus.completed
    job.completed_at = datetime.utcnow()
    db.commit()
    db.refresh(job)
    return job


def cancel_import(db: Session, job_id: uuid.UUID) -> ImportJob:
    job = db.query(ImportJob).filter(ImportJob.id == job_id).first()
    if not job:
        raise ValueError("Import job not found")
    job.status = ImportJobStatus.cancelled
    db.commit()
    db.refresh(job)
    return job


# ── Business-table ingestion (called by confirm_import) ──

_SPACE_STATUS_MAP = {
    "待租": "available", "空置": "available", "available": "available",
    "已租": "leased", "在租": "leased", "leased": "leased",
    "预留": "reserved", "reserved": "reserved",
    "停用": "inactive", "inactive": "inactive",
}
_SPACE_TYPE_MAP = {
    "办公室": "office", "办公": "office", "office": "office",
    "会议室": "meeting", "meeting": "meeting",
    "工位": "coworking", "coworking": "coworking",
    "仓库": "storage", "storage": "storage",
    "零售": "retail", "retail": "retail",
    "办公套件": "office",
}
_TENANT_STATUS_MAP = {
    "活跃": "active", "active": "active", "正常": "active", "履约中": "active",
    "欠费": "overdue", "overdue": "overdue",
    "注销": "inactive", "inactive": "inactive", "停用": "inactive",
}
_TENANT_TYPE_MAP = {
    "企业": "enterprise", "enterprise": "enterprise", "公司": "enterprise",
    "个人": "individual", "individual": "individual",
    "非营利": "ngo", "ngo": "ngo",
    "其他": "other", "other": "other",
}
_LEASE_STATUS_MAP = {
    "生效中": "active", "履行中": "active", "active": "active", "在租": "active",
    "待生效": "pending", "pending": "pending",
    "已到期": "expired", "到期": "expired", "expired": "expired",
    "已终止": "terminated", "终止": "terminated", "terminated": "terminated",
    "已退租": "vacated", "退租": "vacated", "vacated": "vacated",
    "已续签": "renewed", "续签": "renewed", "renewed": "renewed",
}
_PAYMENT_CYCLE_MAP = {
    "月付": "monthly", "月": "monthly", "monthly": "monthly",
    "季付": "quarterly", "季度": "quarterly", "quarterly": "quarterly",
    "半年付": "half_year", "half_year": "half_year",
    "年付": "yearly", "年": "yearly", "yearly": "yearly",
    "一次性": "one_time", "one_time": "one_time",
}


def _lookup_enum(value: Any, mapping: dict[str, str]) -> str | None:
    if value is None:
        return None
    v = str(value).strip()
    return mapping.get(v.lower()) or mapping.get(v)


def _parse_date_value(v: Any) -> str | None:
    """Robust date parsing for ingestion (normalized values may still be strings)."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    from datetime import date as _date
    if isinstance(v, _date):
        return v.isoformat()
    s = str(v).strip().replace("/", "-").replace(".", "-")
    try:
        return _date.fromisoformat(s).isoformat()
    except ValueError:
        # Excel serial number (days since 1899-12-30)
        try:
            n = float(s)
            if 20000 < n < 60000:
                from datetime import timedelta
                return (_date(1899, 12, 30) + timedelta(days=int(n))).isoformat()
        except ValueError:
            pass
    return None


def _parse_amount_value(v: Any) -> float | None:
    if v is None or v == "":
        return None
    s = str(v).replace(",", "").replace("¥", "").replace("￥", "").replace(" ", "").strip()
    try:
        return float(Decimal(s))
    except (InvalidOperation, ValueError):
        return None


def ingest_business_tables(db: Session, job: ImportJob) -> dict[str, int]:
    """Write validated import rows into Space / Tenant business tables.

    Raw import_rows are NEVER modified or deleted — business rows link back via source_import_row_id.
    """
    from ..models import Space, SpaceStatus, SpaceType, Tenant, TenantStatus, TenantType, Building, Lease, LeaseStatus, PaymentCycle

    results: dict[str, int] = {
        "space_created": 0, "space_updated": 0,
        "tenant_created": 0, "tenant_updated": 0, "tenant_skipped": 0,
        "lease_created": 0, "lease_updated": 0, "lease_skipped": 0,
    }

    sheets = db.query(ImportSheet).filter(ImportSheet.import_job_id == job.id).all()
    building_id = None
    b = db.query(Building).filter(Building.project_id == job.project_id).first()
    if b:
        building_id = b.id

    # In-memory upsert caches — the session runs with autoflush=False, so newly
    # added objects are invisible to queries until commit. The same business key
    # can appear in multiple sheets (e.g. 空间台账 + 租赁合同 both carry 空间编号),
    # so cache records by key to keep upserts idempotent within one ingest run.
    space_cache: dict[str, Any] = {}
    tenant_cache: dict[str, Any] = {}
    lease_cache: dict[str, Any] = {}

    for sheet in sheets:
        mappings = db.query(FieldMapping).filter(FieldMapping.import_sheet_id == sheet.id).all()
        by_entity: dict[str, list[FieldMapping]] = {}
        for m in mappings:
            if m.target_entity and m.target_field_name:
                by_entity.setdefault(m.target_entity, []).append(m)

        for entity, emaps in by_entity.items():
            rows = (
                db.query(ImportRow)
                .filter(
                    ImportRow.import_sheet_id == sheet.id,
                    ImportRow.validation_status == ImportRowValidationStatus.valid,
                )
                .all()
            )
            smap = {m.source_field_name: (m.target_field_name, m.target_type) for m in emaps}
            # Sheet-wide map across ALL entity groups — a lease row's tenant/space
            # columns are usually mapped to the tenant/space groups, not to lease.
            sheet_smap = {m.source_field_name: m.target_field_name for m in mappings if m.target_field_name}

            for row in rows:
                vals = row.normalized_values or {}

                if entity == "space":
                    kwargs: dict[str, Any] = {
                        "project_id": job.project_id,
                        "organization_id": job.organization_id,
                        "building_id": building_id,
                        "source_import_row_id": row.id,
                    }
                    for src, (tgt, _ttype) in smap.items():
                        v = vals.get(src)
                        if tgt == "space_code":
                            if v and str(v).strip():
                                kwargs["space_code"] = str(v).strip()
                        elif tgt in ("floor", "area_sqm", "workstation_count"):
                            if v not in (None, ""):
                                kwargs[tgt] = v
                        elif tgt == "status":
                            mapped = _lookup_enum(v, _SPACE_STATUS_MAP)
                            if mapped:
                                kwargs["status"] = SpaceStatus(mapped)
                        elif tgt == "space_type":
                            mapped = _lookup_enum(v, _SPACE_TYPE_MAP)
                            if mapped:
                                kwargs["space_type"] = SpaceType(mapped)
                        elif tgt == "rent_amount":
                            pass  # rent_amount lives on Lease, not Space; ignore here

                    if not kwargs.get("space_code"):
                        continue

                    existing = space_cache.get(kwargs["space_code"])
                    if existing is None:
                        existing = (
                            db.query(Space)
                            .filter(Space.project_id == job.project_id, Space.space_code == kwargs["space_code"])
                            .first()
                        )
                    if existing:
                        for k, v in kwargs.items():
                            if k not in ("project_id", "organization_id", "space_code") and v is not None:
                                setattr(existing, k, v)
                                existing.version = (existing.version or 1) + 1
                        space_cache[kwargs["space_code"]] = existing
                        results["space_updated"] += 1
                    else:
                        new_space = Space(**kwargs)
                        db.add(new_space)
                        space_cache[kwargs["space_code"]] = new_space
                        results["space_created"] += 1

                elif entity == "tenant":
                    kwargs = {
                        "project_id": job.project_id,
                        "organization_id": job.organization_id,
                    }
                    for src, (tgt, _ttype) in smap.items():
                        v = vals.get(src)
                        if tgt in ("tenant_name", "contact_name", "industry", "registration_code"):
                            if v not in (None, ""):
                                kwargs[tgt] = str(v).strip()
                        elif tgt in ("contact_phone", "contact_phone_encrypted"):
                            if v not in (None, ""):
                                kwargs["contact_phone_encrypted"] = str(v).strip()
                        elif tgt == "tenant_type":
                            mapped = _lookup_enum(v, _TENANT_TYPE_MAP)
                            if mapped:
                                kwargs["tenant_type"] = TenantType(mapped)
                        elif tgt == "status":
                            mapped = _lookup_enum(v, _TENANT_STATUS_MAP)
                            if mapped:
                                kwargs["status"] = TenantStatus(mapped)

                    if not kwargs.get("tenant_name"):
                        results["tenant_skipped"] += 1
                        continue
                    existing = tenant_cache.get(kwargs["tenant_name"])
                    if existing is None:
                        existing = (
                            db.query(Tenant)
                            .filter(Tenant.project_id == job.project_id, Tenant.tenant_name == kwargs["tenant_name"])
                            .first()
                        )
                    if existing:
                        for k, v in kwargs.items():
                            if k not in ("project_id", "organization_id", "tenant_name") and v is not None:
                                setattr(existing, k, v)
                        tenant_cache[kwargs["tenant_name"]] = existing
                        results["tenant_updated"] += 1
                    else:
                        new_tenant = Tenant(**kwargs)
                        db.add(new_tenant)
                        tenant_cache[kwargs["tenant_name"]] = new_tenant
                        results["tenant_created"] += 1

                elif entity == "lease":
                    kwargs = {
                        "project_id": job.project_id,
                        "organization_id": job.organization_id,
                        "source_import_row_id": row.id,
                    }
                    tenant_name = None
                    space_code = None
                    for src, (tgt, _ttype) in smap.items():
                        v = vals.get(src)
                        if v in (None, ""):
                            continue
                        if tgt == "lease_code":
                            kwargs["lease_code"] = str(v).strip()
                        elif tgt in ("start_date", "contract_signed_date", "end_date"):
                            d = _parse_date_value(v)
                            if d:
                                kwargs[tgt] = d
                        elif tgt in ("rent_amount", "deposit_amount"):
                            amt = _parse_amount_value(v)
                            if amt is not None:
                                kwargs[tgt] = amt
                        elif tgt == "status":
                            mapped = _lookup_enum(v, _LEASE_STATUS_MAP)
                            if mapped:
                                kwargs["status"] = LeaseStatus(mapped)
                        elif tgt == "payment_cycle":
                            mapped = _lookup_enum(v, _PAYMENT_CYCLE_MAP)
                            if mapped:
                                kwargs["payment_cycle"] = PaymentCycle(mapped)

                    # Tenant / space linkage columns live in other entity groups of
                    # the same sheet — resolve them from the sheet-wide mapping.
                    for src, tgt in sheet_smap.items():
                        if tgt in ("tenant_name", "tenant") and tenant_name is None:
                            v = vals.get(src)
                            if v not in (None, ""):
                                tenant_name = str(v).strip()
                        elif tgt in ("space_code", "space") and space_code is None:
                            v = vals.get(src)
                            if v not in (None, ""):
                                space_code = str(v).strip()

                    if not kwargs.get("lease_code"):
                        results["lease_skipped"] += 1
                        continue

                    # Resolve tenant by name (within project)
                    tenant_id = None
                    if tenant_name:
                        t = tenant_cache.get(tenant_name)
                        if t is None:
                            t = (
                                db.query(Tenant)
                                .filter(Tenant.project_id == job.project_id, Tenant.tenant_name == tenant_name)
                                .first()
                            )
                        if t:
                            tenant_id = t.id
                            tenant_cache[tenant_name] = t
                        else:
                            tenant_id = uuid.uuid4()
                            new_t = Tenant(
                                id=tenant_id,
                                organization_id=job.organization_id,
                                project_id=job.project_id,
                                tenant_name=tenant_name,
                            )
                            db.add(new_t)
                            tenant_cache[tenant_name] = new_t
                            results["tenant_created"] += 1
                    if tenant_id:
                        kwargs["tenant_id"] = tenant_id

                    # Resolve space by code (within project)
                    if space_code:
                        s = space_cache.get(space_code)
                        if s is None:
                            s = (
                                db.query(Space)
                                .filter(Space.project_id == job.project_id, Space.space_code == space_code)
                                .first()
                            )
                        if s:
                            space_cache[space_code] = s
                            kwargs["space_id"] = s.id

                    existing = lease_cache.get(kwargs["lease_code"])
                    if existing is None:
                        existing = (
                            db.query(Lease)
                            .filter(Lease.project_id == job.project_id, Lease.lease_code == kwargs["lease_code"])
                            .first()
                        )
                    if existing:
                        for k, v in kwargs.items():
                            if k not in ("project_id", "organization_id", "lease_code") and v is not None:
                                setattr(existing, k, v)
                        lease_cache[kwargs["lease_code"]] = existing
                        results["lease_updated"] += 1
                    else:
                        # Required fields fallbacks so the row can never violate NOT NULL
                        kwargs.setdefault("tenant_id", None)
                        kwargs.setdefault("space_id", None)
                        if kwargs["tenant_id"] is None or kwargs["space_id"] is None:
                            # Cannot create a lease without its tenant/space linkage
                            results["lease_skipped"] += 1
                            continue
                        kwargs.setdefault("start_date", datetime.utcnow().date().isoformat())
                        kwargs.setdefault("end_date", (datetime.utcnow().date().replace(year=datetime.utcnow().date().year + 1)).isoformat())
                        kwargs.setdefault("rent_amount", 0)
                        kwargs.setdefault("deposit_amount", 0)
                        new_lease = Lease(**kwargs)
                        db.add(new_lease)
                        lease_cache[kwargs["lease_code"]] = new_lease
                        results["lease_created"] += 1

    db.commit()
    return results
