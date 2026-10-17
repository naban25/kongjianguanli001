"""Scheduler 管理端点。

暴露在 /api/v1/admin/scheduler/* 下，提供：
- GET  /status        — 当前调度器状态
- GET  /jobs          — 列出已注册任务
- POST /jobs/{id}/trigger — 立即触发（手动运行）

所有端点要求 is_system_admin 权限。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ...core.auth import AppUser, CurrentUser
from ...core.database import SessionLocal
from ...models import AuditLog
from ...services import scheduler as scheduler_svc
from ...services.scheduler import (
    clean_expired_export_files,
    expire_overdue_leases,
    sync_apitable_for_all_projects,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/scheduler", tags=["scheduler"])


def _require_admin(user: AppUser) -> None:
    if not user.is_system_admin:
        raise HTTPException(status_code=403, detail="仅系统管理员可访问。")


@router.get("/status")
def scheduler_status(user: AppUser = Depends(CurrentUser())) -> dict[str, Any]:
    """返回调度器整体状态：是否运行在当前 worker、已注册任务列表。"""
    _require_admin(user)
    return scheduler_svc.get_scheduler_info()


@router.get("/jobs")
def scheduler_jobs(user: AppUser = Depends(CurrentUser())) -> dict[str, Any]:
    """仅返回任务列表，便于前端 UI 渲染。"""
    _require_admin(user)
    info = scheduler_svc.get_scheduler_info()
    # 过滤掉内部 snapshot 刷新任务（id 以 __ 开头）
    visible_jobs = [j for j in info.get("jobs", []) if not j.get("id", "").startswith("__")]
    return {
        "running": info["running"],
        "primary_worker": info["primary_worker"],
        "current_pid": info["current_pid"],
        "primary_pid": info.get("primary_pid") or info.get("scheduler_pid"),
        "snapshot_age_seconds": info.get("snapshot_age_seconds"),
        "jobs": visible_jobs,
    }


@router.post("/jobs/{job_id}/trigger")
async def scheduler_trigger(
    job_id: str,
    user: AppUser = Depends(CurrentUser()),
) -> dict[str, Any]:
    """立即手动执行某个 job。可在任何 worker 上调用（不依赖当前进程是否主 worker）。

    Returns:
        与该 job 执行结果一致的字典（job_id / ok / 业务字段）。
    """
    _require_admin(user)

    job_map = {
        "expire_overdue_leases": expire_overdue_leases,
        "clean_expired_export_files": clean_expired_export_files,
        "sync_apitable_for_all_projects": sync_apitable_for_all_projects,
    }
    func = job_map.get(job_id)
    if not func:
        raise HTTPException(status_code=404, detail=f"未知任务: {job_id}")

    try:
        result = await func()
    except Exception as e:
        logger.exception("手动触发任务失败: %s", e)
        raise HTTPException(status_code=500, detail=f"触发失败: {e}")

    # 记录审计日志
    try:
        with SessionLocal() as db:
            db.add(AuditLog(
                actor_user_id=user.id,
                action="scheduler_manual_trigger",
                resource_type="scheduler_job",
                resource_id=job_id,
                before_summary=None,
                after_summary={
                    "result_ok": bool(result.get("ok")),
                    "result_keys": list(result.keys())[:10],
                },
            ))
            db.commit()
    except Exception as e:
        logger.warning("记录 scheduler 审计日志失败: %s", e)

    return result
