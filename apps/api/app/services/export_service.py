"""导出服务：业务数据 / 多维表格 → Excel / CSV。

复用 tables.py 的查询能力，支持：
- 全部导出 / 按筛选条件导出
- 选择导出字段
- 异步生成（ExportJob）+ 下载链接

导出文件保存到 storage_backend=local 下的 exports/ 子目录。
"""
from __future__ import annotations

import csv
import io
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from fastapi import HTTPException
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..models import ExportJob, ExportJobStatus, Project

logger = logging.getLogger(__name__)

# 中文表头友好样式
_HEADER_FILL = PatternFill(start_color="FFF7E6", end_color="FFF7E6", fill_type="solid")
_HEADER_FONT = Font(bold=True, color="8C4A1F")
_HEADER_ALIGN = Alignment(horizontal="center", vertical="center")


def render_table_to_excel(
    rows: list[dict],
    columns: list[dict],
    sheet_name: str = "数据",
) -> bytes:
    """将记录列表渲染为 .xlsx 二进制内容。
    columns: [{name, type, label?, enum_map?}, ...]
    rows: [{col_name: value, ...}, ...]
    """
    wb = Workbook()
    # Excel 不允许工作表名包含 : \ / ? * [ ] ，且长度不超过 31
    safe_name = sheet_name.translate(str.maketrans({c: "_" for c in ":\\/?*[]"}))[:31]
    if not safe_name:
        safe_name = "数据"
    ws = wb.active
    ws.title = safe_name

    headers = []
    for col in columns:
        label = col.get("label") or col["name"]
        headers.append(label)
    ws.append(headers)

    # 表头样式
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = _HEADER_ALIGN

    # 数据行
    for row in rows:
        line = []
        for col in columns:
            name = col["name"]
            ctype = col.get("type", "text")
            v = row.get(name)
            if v is None or v == "":
                line.append("")
            elif ctype == "enum":
                enum_map = col.get("enum_map") or {}
                line.append(enum_map.get(str(v), str(v)))
            elif ctype in ("date", "datetime"):
                # openpyxl 接受 datetime/date 对象；str 直接写
                line.append(v)
            elif ctype in ("number", "currency"):
                try:
                    line.append(float(v))
                except (TypeError, ValueError):
                    line.append(v)
            else:
                line.append(str(v) if not isinstance(v, str) else v)
        ws.append(line)

    # 列宽自适应（粗略）
    for idx, col in enumerate(columns, start=1):
        label = col.get("label") or col["name"]
        max_len = len(str(label))
        for row in rows[:50]:
            v = row.get(col["name"])
            if v is not None:
                max_len = max(max_len, len(str(v)))
        ws.column_dimensions[get_column_letter(idx)].width = min(max(max_len + 2, 10), 40)

    # 冻结表头
    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def render_table_to_csv(
    rows: list[dict],
    columns: list[dict],
) -> bytes:
    """将记录列表渲染为 UTF-8 BOM CSV（Excel 打开不乱码）。"""
    buf = io.StringIO()
    writer = csv.writer(buf)

    headers = [col.get("label") or col["name"] for col in columns]
    writer.writerow(headers)

    for row in rows:
        line = []
        for col in columns:
            name = col["name"]
            ctype = col.get("type", "text")
            v = row.get(name)
            if v is None:
                line.append("")
            elif ctype == "enum":
                enum_map = col.get("enum_map") or {}
                line.append(enum_map.get(str(v), str(v)))
            else:
                line.append(str(v) if not isinstance(v, str) else v)
        writer.writerow(line)

    # UTF-8 BOM
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def create_export_job(
    db: Session,
    *,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    table_ref: str,
    columns: list[dict],
    rows: list[dict],
    fmt: str,
    selected_fields: list[str] | None = None,
    filters: dict | None = None,
) -> ExportJob:
    """同步渲染并落盘，返回 ExportJob 记录（含 storage_key）。"""
    settings = get_settings()
    if fmt not in ("xlsx", "csv"):
        raise HTTPException(status_code=400, detail=f"不支持的导出格式: {fmt}")

    # 按 selected_fields 过滤列
    if selected_fields:
        columns = [c for c in columns if c["name"] in selected_fields]

    if fmt == "xlsx":
        content = render_table_to_excel(rows, columns, sheet_name=table_ref)
        ext = "xlsx"
        mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        content = render_table_to_csv(rows, columns)
        ext = "csv"
        mime = "text/csv"

    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在。")

    job_id = uuid.uuid4()
    storage_key = f"exports/{project_id}/{job_id}.{ext}"
    abs_path = settings.local_storage_abs / storage_key
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(content)

    expires_at = datetime.now(timezone.utc) + timedelta(hours=settings.export_ttl_hours)

    job = ExportJob(
        id=job_id,
        organization_id=project.organization_id,
        project_id=project_id,
        requested_by=user_id,
        filters=filters,
        selected_fields=selected_fields,
        status=ExportJobStatus.completed,
        storage_key=storage_key,
        row_count=len(rows),
        expires_at=expires_at,
        completed_at=datetime.now(timezone.utc),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    logger.info("[export] job %s → %s (%d rows, %.1f KB)",
                job_id, storage_key, len(rows), len(content) / 1024)
    return job


def read_export_file(storage_key: str) -> tuple[bytes, str, str]:
    """读取导出文件 → (content, mime, filename)。"""
    settings = get_settings()
    abs_path = settings.local_storage_abs / storage_key
    if not abs_path.exists():
        raise HTTPException(status_code=404, detail="导出文件不存在或已过期。")

    if storage_key.endswith(".xlsx"):
        mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ext = "xlsx"
    else:
        mime = "text/csv"
        ext = "csv"

    return abs_path.read_bytes(), mime, ext