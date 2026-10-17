"""Import pipeline routes: upload → parse → mapping → preview → confirm."""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ...core.auth import CurrentUser, require_project_scope
from ...core.database import get_db
from ...core.org_context import ensure_project_in_active_org
from ...models import (
    AppUser,
    AuditLog,
    FieldMapping,
    ImportJob,
    ImportRow,
    ImportSheet,
    Organization,
    Project,
)
from ...services import import_pipeline

router = APIRouter(prefix="/imports", tags=["imports"])


# ---------- Schemas ----------

class UpdateMappingRequest(BaseModel):
    target_field_name: Optional[str] = None
    target_entity: Optional[str] = None
    target_type: Optional[str] = None


# ---------- Helpers ----------

def _serialize_job(j: ImportJob) -> dict:
    return {
        "id": str(j.id),
        "project_id": str(j.project_id),
        "uploaded_by": str(j.uploaded_by) if j.uploaded_by else None,
        "original_filename": j.original_filename,
        "storage_key": j.storage_key,
        "sha256": j.sha256,
        "file_size": j.file_size,
        "status": j.status.value if hasattr(j.status, "value") else str(j.status),
        "sheet_count": j.sheet_count,
        "row_count": j.row_count,
        "success_count": j.success_count,
        "error_count": j.error_count,
        "created_at": j.created_at.isoformat(),
        "completed_at": j.completed_at.isoformat() if j.completed_at else None,
    }


def _serialize_sheet(s: ImportSheet, db: Session) -> dict:
    mappings = db.query(FieldMapping).filter(FieldMapping.import_sheet_id == s.id).all()
    return {
        "id": str(s.id),
        "sheet_name": s.sheet_name,
        "header_row_index": s.header_row_index,
        "row_count": s.row_count,
        "column_count": s.column_count,
        "status": s.status,
        "headers": [m.source_field_name for m in mappings],
        "mapping": [
            {
                "id": str(m.id),
                "source_field_name": m.source_field_name,
                "target_field_name": m.target_field_name,
                "target_entity": m.target_entity,
                "target_type": m.target_type,
                "confidence": float(m.confidence) if m.confidence is not None else None,
            }
            for m in mappings
        ],
    }


# ---------- Endpoints ----------

@router.post("", response_model=dict)
async def upload_file(
    project_id: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: AppUser = Depends(CurrentUser()),
):
    """上传并解析 Excel/CSV。"""
    if not require_project_scope(project_id, user, db):
        raise HTTPException(status_code=403, detail="无权导入到该项目。")

    try:
        pid = uuid.UUID(project_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的项目 ID。")

    proj = db.query(Project).filter(Project.id == pid).first()
    if not proj:
        raise HTTPException(status_code=404, detail="项目不存在。")

    org_id = ensure_project_in_active_org(pid, user, db)

    content = await file.read()
    job = import_pipeline.start_import_job(
        db,
        project_id=pid,
        org_id=org_id,
        user_id=user.id,
        filename=file.filename or "upload",
        content=content,
    )

    db.add(
        AuditLog(
            actor_user_id=user.id,
            action="import_upload",
            resource_type="import_job",
            resource_id=str(job.id),
            project_id=pid,
            request_id=getattr(req.state, "request_id", None) if req else None,
        )
    )
    db.commit()

    return {"ok": True, "job": _serialize_job(job)}


@router.get("")
def list_imports(
    project_id: str = Query(...),
    db: Session = Depends(get_db),
    user: AppUser = Depends(CurrentUser()),
):
    if not require_project_scope(project_id, user, db):
        raise HTTPException(status_code=403, detail="无权访问此项目。")
    pid = uuid.UUID(project_id)
    ensure_project_in_active_org(pid, user, db)
    rows = db.query(ImportJob).filter(ImportJob.project_id == pid).order_by(ImportJob.created_at.desc()).all()
    return {"items": [_serialize_job(j) for j in rows]}


@router.get("/{job_id}")
def get_import(job_id: str, request: Request = None, db: Session = Depends(get_db), user: AppUser = Depends(CurrentUser())):
    try:
        jid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的导入 ID。")
    j = db.query(ImportJob).filter(ImportJob.id == jid).first()
    if not j:
        raise HTTPException(status_code=404, detail="导入批次不存在。")
    if not require_project_scope(str(j.project_id), user, db):
        raise HTTPException(status_code=403, detail="无权访问此项目。")
    ensure_project_in_active_org(j.project_id, user, db)
    return _serialize_job(j)


@router.get("/{job_id}/sheets")
def get_import_sheets(job_id: str, request: Request = None, db: Session = Depends(get_db), user: AppUser = Depends(CurrentUser())):
    try:
        jid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的导入 ID。")
    j = db.query(ImportJob).filter(ImportJob.id == jid).first()
    if not j:
        raise HTTPException(status_code=404, detail="导入批次不存在。")
    if not require_project_scope(str(j.project_id), user, db):
        raise HTTPException(status_code=403, detail="无权访问此项目。")
    ensure_project_in_active_org(j.project_id, user, db)

    sheets = db.query(ImportSheet).filter(ImportSheet.import_job_id == jid).all()
    return {"items": [_serialize_sheet(s, db) for s in sheets]}


@router.get("/sheets/{sheet_id}/preview")
def sheet_preview(
    sheet_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: AppUser = Depends(CurrentUser()),
):
    """Return paginated raw values + status for a sheet."""
    try:
        sid = uuid.UUID(sheet_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的工作表 ID。")
    sheet = db.query(ImportSheet).filter(ImportSheet.id == sid).first()
    if not sheet:
        raise HTTPException(status_code=404, detail="工作表不存在。")
    job = db.query(ImportJob).filter(ImportJob.id == sheet.import_job_id).first()
    if not require_project_scope(str(job.project_id), user, db):
        raise HTTPException(status_code=403, detail="无权访问此项目。")
    ensure_project_in_active_org(job.project_id, user, db)

    rows = (
        db.query(ImportRow)
        .filter(ImportRow.import_sheet_id == sid)
        .order_by(ImportRow.source_row_number.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    mappings = db.query(FieldMapping).filter(FieldMapping.import_sheet_id == sid).all()
    headers = [m.source_field_name for m in mappings] or list(rows[0].raw_values.keys()) if rows else []

    return {
        "sheet_name": sheet.sheet_name,
        "headers": headers,
        "items": [
            {
                "source_row_number": r.source_row_number,
                "raw_values": r.raw_values,
                "normalized_values": r.normalized_values,
                "validation_status": r.validation_status.value if hasattr(r.validation_status, "value") else str(r.validation_status),
            }
            for r in rows
        ],
        "page": page,
        "page_size": page_size,
    }


@router.post("/sheets/{sheet_id}/suggest-llm")
def suggest_with_llm(
    sheet_id: str,
    db: Session = Depends(get_db),
    user: AppUser = Depends(CurrentUser()),
) -> dict:
    """使用 LLM 重新建议字段映射，覆盖该 sheet 现有的未确认映射。
    用户已确认的（mapping_method='manual'）保持不变。
    """
    try:
        sid = uuid.UUID(sheet_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的工作表 ID。")

    sheet = db.query(ImportSheet).filter(ImportSheet.id == sid).first()
    if not sheet:
        raise HTTPException(status_code=404, detail="工作表不存在。")
    job = db.query(ImportJob).filter(ImportJob.id == sheet.import_job_id).first()
    if not require_project_scope(str(job.project_id), user, db):
        raise HTTPException(status_code=403, detail="无权访问此项目。")
    ensure_project_in_active_org(job.project_id, user, db)

    # 提取样本值
    rows = (
        db.query(ImportRow)
        .filter(ImportRow.import_sheet_id == sid)
        .limit(20)
        .all()
    )
    field_mappings = (
        db.query(FieldMapping)
        .filter(FieldMapping.import_sheet_id == sid)
        .all()
    )
    samples = import_pipeline.extract_samples(
        [m.source_field_name for m in field_mappings],
        [r.raw_values for r in rows],
        max_samples=5,
    )

    # 调用 LLM 推断
    suggestions = import_pipeline.suggest_mapping_llm(
        [m.source_field_name for m in field_mappings],
        samples,
    )

    # 覆盖未确认的映射（保留 mapping_method='manual' 的）
    updated = 0
    by_src = {s["source_field_name"]: s for s in suggestions}
    for m in field_mappings:
        if m.mapping_method == "manual":
            continue
        s = by_src.get(m.source_field_name)
        if not s:
            continue
        m.target_field_name = s["target_field_name"]
        m.target_entity = s["target_entity"]
        m.target_type = s["target_type"]
        m.confidence = s["confidence"]
        m.mapping_method = s["mapping_method"]
        m.confirmed_by = None
        m.confirmed_at = None
        updated += 1

    # ============ 表格补全建议（P2.1）============
    # 基于当前 sheet 推断结果，提示用户：
    # 1) 未映射源字段（孤立列）→ 建议手动确认或忽略
    # 2) 低置信度字段 → 建议人工核对
    # 3) 核心目标实体必填字段缺失 → 警告
    completion_suggestions: list[dict] = []

    unmapped_sources = [m for m in field_mappings if not (by_src.get(m.source_field_name) or {}).get("target_field_name")]
    for m in unmapped_sources:
        completion_suggestions.append({
            "kind": "unmapped_source",
            "severity": "info",
            "source_field_name": m.source_field_name,
            "message": f"源列「{m.source_field_name}」未匹配到任何业务字段，建议手动指定或忽略。",
        })

    low_conf = [m for m in field_mappings if (by_src.get(m.source_field_name) or {}).get("confidence") is not None
                and float((by_src.get(m.source_field_name) or {}).get("confidence") or 0) < 0.7]
    for m in low_conf:
        s = by_src.get(m.source_field_name) or {}
        completion_suggestions.append({
            "kind": "low_confidence",
            "severity": "warning",
            "source_field_name": m.source_field_name,
            "target_field_name": s.get("target_field_name"),
            "target_entity": s.get("target_entity"),
            "confidence": s.get("confidence"),
            "message": f"「{m.source_field_name}」→「{s.get('target_field_name')}」置信度 {float(s.get('confidence') or 0):.2f}，建议人工确认。",
        })

    # 推断目标实体（按多数投票 — 基于最终生效的映射，包含用户 manual 修改）
    target_entity_counts: dict[str, int] = {}
    final_mappings: list[tuple[str, str | None, str | None]] = [
        (m.source_field_name, m.target_entity, m.target_field_name)
        for m in field_mappings
    ]
    for _, ent, _ in final_mappings:
        if ent:
            target_entity_counts[ent] = target_entity_counts.get(ent, 0) + 1
    inferred_entity = max(target_entity_counts, key=target_entity_counts.get) if target_entity_counts else None

    # 核心必填字段检查 — 基于最终生效的映射（包含用户 manual 修改）
    final_target_keys = {(ent, fld) for _, ent, fld in final_mappings if ent and fld}

    REQUIRED_FIELDS: dict[str, list[tuple[str, str]]] = {
        "space":  [("space_code", "空间编号")],
        "tenant": [("tenant_name", "租户名称")],
        "lease":  [("lease_code", "租约编号"), ("tenant_name", "租户名称"), ("space_code", "空间编号")],
    }
    if inferred_entity and inferred_entity in REQUIRED_FIELDS:
        for fname, flabel in REQUIRED_FIELDS[inferred_entity]:
            if (inferred_entity, fname) not in final_target_keys:
                completion_suggestions.append({
                    "kind": "missing_required",
                    "severity": "error",
                    "target_entity": inferred_entity,
                    "target_field_name": fname,
                    "message": f"目标实体「{inferred_entity}」缺少核心必填字段「{fname}（{flabel}）」，导入将被拒绝。",
                })

    # 重复映射检测 — 基于最终生效的映射
    target_seen: dict[tuple[str, str], list[str]] = {}
    for src, ent, fld in final_mappings:
        if ent and fld:
            target_seen.setdefault((ent, fld), []).append(src)
    for (ent, fname), sources in target_seen.items():
        if len(sources) > 1:
            completion_suggestions.append({
                "kind": "duplicate_target",
                "severity": "warning",
                "target_entity": ent,
                "target_field_name": fname,
                "sources": sources,
                "message": f"目标字段「{ent}.{fname}」被多个源列映射：{', '.join(sources)}，导入时后者将覆盖前者。",
            })

    db.commit()
    return {
        "ok": True,
        "updated_count": updated,
        "suggestions": suggestions,
        "samples": samples,
        "completion": {
            "inferred_entity": inferred_entity,
            "total_issues": len(completion_suggestions),
            "by_severity": {
                sev: sum(1 for s in completion_suggestions if s.get("severity") == sev)
                for sev in ("error", "warning", "info")
            },
            "items": completion_suggestions,
        },
    }


@router.patch("/sheets/{sheet_id}/mapping/{mapping_id}")
def update_mapping(
    sheet_id: str,
    mapping_id: str,
    payload: UpdateMappingRequest,
    db: Session = Depends(get_db),
    user: AppUser = Depends(CurrentUser()),
):
    """用户确认 / 修改字段映射。"""
    try:
        mid = uuid.UUID(mapping_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的映射 ID。")
    m = db.query(FieldMapping).filter(FieldMapping.id == mid).first()
    if not m:
        raise HTTPException(status_code=404, detail="映射不存在。")

    changes = payload.model_dump(exclude_unset=True)
    for k, v in changes.items():
        setattr(m, k, v)
    m.confirmed_by = user.id
    from datetime import datetime, timezone
    m.confirmed_at = datetime.now(timezone.utc)
    m.mapping_method = "manual"
    db.commit()
    return {"ok": True, "id": str(m.id)}


@router.post("/{job_id}/preview")
def run_preview(job_id: str, request: Request = None, db: Session = Depends(get_db), user: AppUser = Depends(CurrentUser())):
    try:
        jid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的导入 ID。")
    j = db.query(ImportJob).filter(ImportJob.id == jid).first()
    if not j:
        raise HTTPException(status_code=404, detail="导入批次不存在。")
    if not require_project_scope(str(j.project_id), user, db):
        raise HTTPException(status_code=403, detail="无权访问此项目。")
    ensure_project_in_active_org(j.project_id, user, db)
    return import_pipeline.run_preview(db, jid)


@router.post("/{job_id}/confirm")
def confirm(job_id: str, req: Request = None, db: Session = Depends(get_db), user: AppUser = Depends(CurrentUser())):
    try:
        jid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的导入 ID。")
    j = db.query(ImportJob).filter(ImportJob.id == jid).first()
    if not j:
        raise HTTPException(status_code=404, detail="导入批次不存在。")
    if not require_project_scope(str(j.project_id), user, db):
        raise HTTPException(status_code=403, detail="无权访问此项目。")
    ensure_project_in_active_org(j.project_id, user, db)

    result = import_pipeline.confirm_import(db, jid)
    db.add(
        AuditLog(
            actor_user_id=user.id,
            action="import_confirm",
            resource_type="import_job",
            resource_id=str(result.id),
            project_id=result.project_id,
            request_id=getattr(req.state, "request_id", None) if req else None,
        )
    )
    db.commit()
    return _serialize_job(result)


@router.post("/{job_id}/cancel")
def cancel(job_id: str, request: Request = None, db: Session = Depends(get_db), user: AppUser = Depends(CurrentUser())):
    try:
        jid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的导入 ID。")
    j = db.query(ImportJob).filter(ImportJob.id == jid).first()
    if not j:
        raise HTTPException(status_code=404, detail="导入批次不存在。")
    if not require_project_scope(str(j.project_id), user, db):
        raise HTTPException(status_code=403, detail="无权访问此项目。")
    ensure_project_in_active_org(j.project_id, user, db)
    return _serialize_job(import_pipeline.cancel_import(db, jid))
