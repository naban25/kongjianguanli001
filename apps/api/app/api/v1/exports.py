"""导出端点：业务表 → Excel/CSV。

POST /exports：创建导出任务（同步渲染，小数据集 < 5k 行）
GET  /exports/{job_id}/download：下载文件
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...core.auth import AppUser, CurrentUser, require_project_scope
from ...core.database import get_db
from ...core.org_context import ensure_project_in_active_org
from ...models import (
    AuditLog,
    ExportJob,
)
from ...services.export_service import (
    create_export_job,
    read_export_file,
)

router = APIRouter(prefix="/exports", tags=["exports"])


class ExportRequest(BaseModel):
    project_id: str
    table_ref: str = Field(..., description="business:spaces / business:tenants / ...")
    format: str = Field("xlsx", pattern="^(xlsx|csv)$")
    selected_fields: Optional[list[str]] = None
    filters: Optional[str] = None  # 同 tables API 的 filters 字符串


@router.post("")
def create_export(
    req: ExportRequest,
    db: Session = Depends(get_db),
    user: AppUser = Depends(CurrentUser()),
) -> dict:
    """同步生成导出文件并落盘，返回 job_id 与下载 URL。"""
    if not require_project_scope(req.project_id, user, db):
        raise HTTPException(status_code=403, detail="无权访问此项目。")
    ensure_project_in_active_org(uuid.UUID(req.project_id), user, db)

    # 复用 tables.py 的查询能力
    from .tables import _business_records, COLUMN_META

    project_id = uuid.UUID(req.project_id)
    # table_ref 形如 "business:spaces"，截取实体部分
    entity = req.table_ref.split(":", 1)[1] if ":" in req.table_ref else req.table_ref
    if entity not in COLUMN_META:
        raise HTTPException(status_code=400, detail=f"不支持的表: {req.table_ref}")

    # 拉取全部数据（不带分页，单次最多 5000 行）
    result = _business_records(
        db, project_id, entity,
        page=1, page_size=5000,
        search=None, sort_by="created_at", sort_dir="asc",
        filters=req.filters,
    )
    rows = result["items"]
    col_meta = COLUMN_META.get(entity, [])

    # selected_fields 限定：必须是 col_meta 中存在的列
    valid_names = {c["name"] for c in col_meta}
    if req.selected_fields:
        bad = [f for f in req.selected_fields if f not in valid_names]
        if bad:
            raise HTTPException(
                status_code=400,
                detail=f"未知字段: {bad}; 允许字段: {sorted(valid_names)}",
            )

    job = create_export_job(
        db,
        project_id=project_id,
        user_id=user.id,
        table_ref=entity,
        columns=col_meta,
        rows=rows,
        fmt=req.format,
        selected_fields=req.selected_fields,
        filters={"raw": req.filters} if req.filters else None,
    )

    # 审计日志
    db.add(AuditLog(
        actor_user_id=user.id,
        action="export_create",
        resource_type=req.table_ref,
        resource_id=str(job.id),
        project_id=project_id,
        after_summary={
            "format": req.format,
            "row_count": job.row_count,
            "selected_fields": req.selected_fields,
        },
    ))
    db.commit()

    return {
        "job_id": str(job.id),
        "format": req.format,
        "row_count": job.row_count,
        "expires_at": job.expires_at.isoformat() if job.expires_at else None,
        "download_url": f"/api/v1/exports/{job.id}/download",
    }


@router.get("/{job_id}/download")
def download_export(
    job_id: str,
    db: Session = Depends(get_db),
    user: AppUser = Depends(CurrentUser()),
) -> Response:
    """下载导出文件。"""
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的 job_id。")

    job = db.query(ExportJob).filter(ExportJob.id == job_uuid).first()
    if not job:
        raise HTTPException(status_code=404, detail="导出任务不存在。")

    if not require_project_scope(str(job.project_id), user, db):
        raise HTTPException(status_code=403, detail="无权访问此项目。")
    ensure_project_in_active_org(job.project_id, user, db)

    content, mime, ext = read_export_file(job.storage_key)
    filename = f"{job.storage_key.split('/')[-1].rsplit('.', 1)[0]}.{ext}"
    return Response(
        content=content,
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
