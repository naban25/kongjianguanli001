"""APScheduler 定时任务服务。

提供 3 个后台任务：
1. expire_overdue_leases  — 每日 02:00 把已过 end_date 但仍 active/pending 的租约置为 expired
2. sync_apitable_for_all_projects — 每小时同步 Neon → APITable（仅在 APITable 已配置时启用）
3. clean_expired_export_files — 每日 03:30 删除超过 export_ttl_hours 的导出文件，并把 ExportJob 标记为 expired

多 Worker 协调
──────────────────────────────────────────────
生产部署使用 gunicorn --workers 4，每个 worker 都加载同一个 app 实例，
默认情况下每个 worker 都会启动一个 APScheduler，导致同一任务被并发触发 4 次。
解决方案：使用文件级 fcntl 排他锁，仅持有锁的 worker 启动 scheduler。
锁在进程死亡（gunicorn worker 重启）时由内核自动释放。

对外 API：
- init_scheduler()    在 FastAPI lifespan 启动时调用
- shutdown_scheduler() 在 lifespan 关闭时调用
- get_scheduler_info() 用于 admin /admin/scheduler/jobs 端点
- trigger_job(job_id) 立即执行任务
"""
from __future__ import annotations

import fcntl
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ..core.config import get_settings
from ..core.database import SessionLocal
from ..models import AuditLog, ExportJob, ExportJobStatus, Lease, LeaseStatus, Project

logger = logging.getLogger(__name__)

# 唯一协调锁路径。生产环境放在 /tmp；本地开发放在 var/ 下，避免权限问题。
SCHEDULER_LOCK_PATH = Path(os.environ.get("SCHEDULER_LOCK_PATH", "/tmp/office_lease_scheduler.lock"))


# ============================================================
#  单 worker 协调（基于 fcntl 文件锁）
# ============================================================

class _SchedulerCoordinator:
    """进程级单例，确保多 gunicorn worker 中仅有一个启动 scheduler。

    使用 fcntl 排他文件锁。锁在进程消失时由内核自动释放。
    """

    def __init__(self) -> None:
        self._fd: Optional[Any] = None
        self._acquired: bool = False
        self.scheduler: Optional[AsyncIOScheduler] = None

    def try_acquire(self) -> bool:
        """非阻塞地尝试获取文件锁。返回是否成功。"""
        try:
            SCHEDULER_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
            self._fd = open(SCHEDULER_LOCK_PATH, "w")
            fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fd.write(f"pid={os.getpid()}\n")
            self._fd.flush()
            self._acquired = True
            return True
        except OSError as e:
            logger.info("[scheduler] 锁已被其他 worker 持有，跳过启动本 worker: %s", e)
            if self._fd:
                try:
                    self._fd.close()
                except Exception:
                    pass
                self._fd = None
            return False

    def release(self) -> None:
        if self._fd:
            try:
                fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                self._fd.close()
            except Exception:
                pass
            self._fd = None
        self._acquired = False


_coordinator = _SchedulerCoordinator()


def is_primary_worker() -> bool:
    """当前进程是否持锁（即负责运行调度任务）。"""
    return _coordinator._acquired


# ============================================================
#  任务定义（都是异步函数）
# ============================================================

async def expire_overdue_leases() -> dict[str, Any]:
    """每日任务：把 end_date < today 且状态仍 active/pending 的租约置为 expired。

    Returns:
        包含受影响行数与执行时间的字典，供 admin 端点展示。
    """
    started_at = datetime.now(timezone.utc)
    today = date.today()
    affected = 0
    try:
        with SessionLocal() as db:
            # 复用相同于 dashboard.reminders 的判定，但反转为「已过期」
            rows = (
                db.query(Lease)
                .filter(
                    Lease.end_date < today,
                    Lease.status.in_([
                        LeaseStatus.active.value,
                        LeaseStatus.pending.value,
                    ]),
                )
                .all()
            )
            for lease in rows:
                lease.status = LeaseStatus.expired
                affected += 1

            if affected:
                db.add(AuditLog(
                    action="scheduler_expire_leases",
                    resource_type="lease",
                    resource_id=None,
                    before_summary=None,
                    after_summary={
                        "affected": affected,
                        "as_of": today.isoformat(),
                        "trigger": "scheduled",
                    },
                ))
            db.commit()
    except Exception as e:
        logger.exception("[scheduler] expire_overdue_leases failed: %s", e)
        return {
            "ok": False,
            "job_id": "expire_overdue_leases",
            "error": str(e),
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }

    finished_at = datetime.now(timezone.utc)
    logger.info("[scheduler] expire_overdue_leases 标记 %d 条租约 → expired (date=%s)",
                affected, today.isoformat())

    # 业务推送：标记了过期租约后对外通知（仅在确有变更时发送，避免噪音）
    if affected > 0:
        try:
            from .notify import notify_event
            await notify_event(
                title=f"过期租约自动标记 {affected} 条",
                summary=(
                    f"日期: {today.isoformat()}\n"
                    f"共 {affected} 条租约因 end_date < today 且状态仍 active/pending，\n"
                    f"已自动置为 expired。详情请查看审计日志 (scheduler_expire_leases)。"
                ),
                level="warning",
            )
        except Exception as e:
            # 通知失败不影响主流程
            logger.warning("[scheduler] notify_event failed (expire_overdue_leases): %s", e)

    return {
        "ok": True,
        "job_id": "expire_overdue_leases",
        "affected": affected,
        "as_of": today.isoformat(),
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_ms": int((finished_at - started_at).total_seconds() * 1000),
    }


async def clean_expired_export_files() -> dict[str, Any]:
    """每日任务：清理超过 export_ttl_hours 的导出文件，并更新 ExportJob.status=expired。"""
    started_at = datetime.now(timezone.utc)
    settings = get_settings()
    now = datetime.now(timezone.utc)
    files_removed = 0
    jobs_expired = 0
    errors: list[str] = []

    try:
        with SessionLocal() as db:
            jobs = (
                db.query(ExportJob)
                .filter(
                    ExportJob.status == ExportJobStatus.completed,
                    ExportJob.expires_at != None,  # noqa: E711
                    ExportJob.expires_at < now,
                )
                .all()
            )
            for job in jobs:
                # 删除磁盘文件
                if job.storage_key:
                    try:
                        path = settings.local_storage_abs / job.storage_key
                        if path.exists():
                            path.unlink()
                            files_removed += 1
                    except Exception as e:
                        errors.append(f"{job.storage_key}: {e}")
                        logger.warning("[scheduler] 删除导出文件失败 %s: %s",
                                       job.storage_key, e)
                job.status = ExportJobStatus.expired
                jobs_expired += 1

            if jobs_expired:
                db.add(AuditLog(
                    action="scheduler_clean_exports",
                    resource_type="export_job",
                    resource_id=None,
                    before_summary=None,
                    after_summary={
                        "jobs_expired": jobs_expired,
                        "files_removed": files_removed,
                    },
                ))
            db.commit()
    except Exception as e:
        logger.exception("[scheduler] clean_expired_export_files failed: %s", e)
        return {
            "ok": False,
            "job_id": "clean_expired_export_files",
            "error": str(e),
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }

    finished_at = datetime.now(timezone.utc)
    logger.info("[scheduler] clean_expired_export_files 标记 %d 个任务 → expired，删除 %d 个文件",
                jobs_expired, files_removed)
    return {
        "ok": True,
        "job_id": "clean_expired_export_files",
        "jobs_expired": jobs_expired,
        "files_removed": files_removed,
        "ttl_hours": settings.export_ttl_hours,
        "errors": errors[:5],  # 只返回前 5 条避免日志爆炸
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_ms": int((finished_at - started_at).total_seconds() * 1000),
    }


async def sync_apitable_for_all_projects() -> dict[str, Any]:
    """每小时任务：把所有项目的 5 类业务实体同步到 APITable。

    若 APITable 未配置（缺 token / space_id）则跳过并返回 skipped=True。
    复用 tables._business_model + apitable_sync.sync_entity 的能力。
    """
    started_at = datetime.now(timezone.utc)
    settings = get_settings()
    if not settings.apitable_configured:
        logger.info("[scheduler] sync_apitable_for_all_projects skipped: APITable 未配置")
        return {
            "ok": True,
            "skipped": True,
            "reason": "APITable 未配置（缺少 APITABLE_TOKEN 或 APITABLE_SPACE_ID）",
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }

    # 延迟导入以避免 scheduler 与 apitable_sync 形成循环
    from .apitable_sync import APITableError, sync_entity
    from ..api.v1.tables import COLUMN_META, _business_model

    # row_to_dict 也定义在 tables.py 中
    from ..api.v1.tables import row_to_dict

    errors: list[dict] = []
    total_entities = 0
    success_entities = 0
    total_written = 0

    ENTITIES = ("spaces", "tenants", "leases", "charges", "payments")

    try:
        with SessionLocal() as db:
            projects = db.query(Project).filter(Project.status == "active").all()
            if not projects:
                projects = db.query(Project).all()  # fallback: 任意状态的项目

        for project in projects:
            for ent in ENTITIES:
                total_entities += 1
                with SessionLocal() as db2:
                    try:
                        ModelClass = _business_model(ent)
                        q = db2.query(ModelClass).filter(ModelClass.project_id == project.id)
                        db_rows = q.all()
                        records = [row_to_dict(r) for r in db_rows]
                        col_meta = COLUMN_META.get(ent, [])
                        result = await sync_entity(ent, records, col_meta)
                        total_written += result.get("written", 0)
                        success_entities += 1
                    except APITableError as e:
                        errors.append({
                            "project_id": str(project.id),
                            "entity": ent,
                            "error": str(e),
                            "code": e.code,
                        })
                        logger.warning("[scheduler] %s/%s 同步失败: %s", project.id, ent, e)
                    except Exception as e:
                        errors.append({
                            "project_id": str(project.id),
                            "entity": ent,
                            "error": str(e),
                        })
                        logger.exception("[scheduler] %s/%s 同步异常", project.id, ent)

        # 记录总体审计日志
        with SessionLocal() as db3:
            db3.add(AuditLog(
                action="scheduler_apitable_sync",
                resource_type="apitable",
                resource_id=None,
                before_summary=None,
                after_summary={
                    "total_entities": total_entities,
                    "success_entities": success_entities,
                    "total_written": total_written,
                    "errors": len(errors),
                },
            ))
            db3.commit()
    except Exception as e:
        logger.exception("[scheduler] sync_apitable_for_all_projects failed: %s", e)
        return {
            "ok": False,
            "job_id": "sync_apitable_for_all_projects",
            "error": str(e),
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }

    finished_at = datetime.now(timezone.utc)
    logger.info("[scheduler] sync_apitable_for_all_projects: %d/%d 实体成功，共写入 %d 条",
                success_entities, total_entities, total_written)
    return {
        "ok": len(errors) == 0,
        "job_id": "sync_apitable_for_all_projects",
        "skipped": False,
        "total_entities": total_entities,
        "success_entities": success_entities,
        "total_written": total_written,
        "errors": errors[:10],
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_ms": int((finished_at - started_at).total_seconds() * 1000),
    }


# ============================================================
#  Scheduler 生命周期
# ============================================================

JOB_DEFINITIONS = [
    {
        "id": "expire_overdue_leases",
        "func": expire_overdue_leases,
        "trigger_args": {"trigger": "cron", "hour": 2, "minute": 0},
        "description": "每日 02:00 把 end_date < today 的 active/pending 租约置为 expired",
    },
    {
        "id": "clean_expired_export_files",
        "func": clean_expired_export_files,
        "trigger_args": {"trigger": "cron", "hour": 3, "minute": 30},
        "description": "每日 03:30 清理过期导出文件（>export_ttl_hours）",
    },
    {
        "id": "sync_apitable_for_all_projects",
        "func": sync_apitable_for_all_projects,
        "trigger_args": {"trigger": "interval", "minutes": 60},
        "description": "每小时同步 Neon → APITable（仅在 APITable 已配置时启用）",
    },
]


def init_scheduler() -> Optional[AsyncIOScheduler]:
    """初始化 APScheduler。仅在持有文件锁的进程中启动。

    Returns:
        启动的 scheduler 实例，或 None（表示本进程未获取锁，不需启动）。
    """
    if _coordinator.scheduler is not None:
        return _coordinator.scheduler

    if not _coordinator.try_acquire():
        logger.info(
            "[scheduler] worker pid=%d 未持有调度锁，其他 worker 已运行 scheduler",
            os.getpid(),
        )
        return None

    settings = get_settings()
    scheduler = AsyncIOScheduler(timezone=settings.app_timezone)

    # 添加定期刷新 snapshot 的子任务（每 30 秒写一次 DB snapshot，让其他 worker 看得到）
    def _refresh_snapshot():
        try:
            snapshot = {
                "primary_pid": os.getpid(),
                "scheduler_pid": os.getpid(),
                "running": scheduler.running,
                "jobs": [
                    {
                        "id": j.id,
                        "name": j.name,
                        "next_run_time": j.next_run_time.isoformat() if j.next_run_time else None,
                        "trigger": str(j.trigger),
                    }
                    for j in scheduler.get_jobs()
                ],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            _persist_state_snapshot(snapshot)
        except Exception as e:
            logger.debug("[scheduler] 刷新 snapshot 失败: %s", e)

    # 注册每 30 秒执行一次的状态刷新
    try:
        scheduler.add_job(
            _refresh_snapshot,
            "interval",
            seconds=30,
            id="__refresh_state_snapshot",
            name="(内部) 刷新 scheduler_state 快照",
            max_instances=1,
            coalesce=True,
        )
    except Exception as e:
        logger.warning("[scheduler] 注册 snapshot 刷新任务失败: %s", e)

    for job_def in JOB_DEFINITIONS:
        # apitable 任务仅在配置存在时启用
        if job_def["id"] == "sync_apitable_for_all_projects" and not settings.apitable_configured:
            logger.info("[scheduler] 跳过注册 %s：APITable 未配置", job_def["id"])
            continue

        kwargs = dict(job_def["trigger_args"])
        trigger_name = kwargs.pop("trigger")
        scheduler.add_job(
            job_def["func"],
            trigger=trigger_name,
            id=job_def["id"],
            name=job_def["description"],
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
            **kwargs,
        )

    try:
        scheduler.start()
    except Exception as e:
        logger.exception("[scheduler] 启动失败: %s", e)
        _coordinator.release()
        return None

    _coordinator.scheduler = scheduler

    # 启动后立即写一次 snapshot（即使没有 job 触发，其他 worker 也能立刻读到状态）
    try:
        _refresh_snapshot()
        logger.info("[scheduler] 初始 snapshot 已落库")
    except Exception as e:
        logger.debug("[scheduler] 初始 snapshot 落库失败: %s", e)

    logger.info(
        "[scheduler] 启动成功 (pid=%d) — 已注册 %d 个任务: %s",
        os.getpid(),
        len(scheduler.get_jobs()),
        [j.id for j in scheduler.get_jobs() if not j.id.startswith("__")],
    )
    return scheduler


def shutdown_scheduler() -> None:
    """关闭 scheduler 并释放锁。"""
    if _coordinator.scheduler is not None:
        try:
            _coordinator.scheduler.shutdown(wait=False)
        except Exception as e:
            logger.warning("[scheduler] 关闭时异常: %s", e)
        _coordinator.scheduler = None
    _coordinator.release()
    logger.info("[scheduler] scheduler 已关闭 (pid=%d)", os.getpid())


def get_scheduler_info() -> dict[str, Any]:
    """提供状态给 admin 端点。

    如果本进程就是主 worker，则读取本地 scheduler 实例的实时状态。
    否则读 DB 中主 worker 写入的 snapshot（_persist_state_interval_seconds 内有效）。
    这样所有 worker 都能返回一致的状态。

    返回的 jobs 已经过滤掉内部 __refresh_state_snapshot 任务。
    """
    def _filter_visible_jobs(jobs: list[dict]) -> list[dict]:
        """过滤掉内部 __ 开头的辅助任务。"""
        return [j for j in jobs if not j.get("id", "").startswith("__")]

    # 主 worker 直接读本地 scheduler
    if _coordinator.scheduler is not None and _coordinator._acquired:
        scheduler = _coordinator.scheduler
        jobs: list[dict] = []
        for job in scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "name": job.name,
                "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
                "trigger": str(job.trigger),
            })

        snapshot = {
            "primary_worker": True,
            "running": scheduler.running,
            "scheduler_pid": os.getpid(),
            "current_pid": os.getpid(),
            "jobs": _filter_visible_jobs(jobs),
            "primary_pid": os.getpid(),
        }
        # 异步落库快照，供其他 worker 读取（失败不影响响应）
        try:
            _persist_state_snapshot(snapshot)
        except Exception as e:
            logger.debug("[scheduler] 持久化 snapshot 失败: %s", e)
        return snapshot

    # 非主 worker：尝试读 DB snapshot，过期则兜底返回本地视图
    db_snapshot = _read_state_snapshot()
    if db_snapshot and (datetime.now(timezone.utc) - db_snapshot["snapshot_at"]).total_seconds() < 300:
        return {
            "primary_worker": False,
            "running": db_snapshot["running"],
            "scheduler_pid": db_snapshot["primary_pid"],
            "current_pid": os.getpid(),
            "jobs": _filter_visible_jobs(db_snapshot["jobs"]),
            "primary_pid": db_snapshot["primary_pid"],
            "snapshot_age_seconds": int((datetime.now(timezone.utc) - db_snapshot["snapshot_at"]).total_seconds()),
        }

    # 兜底：本进程未启动 scheduler 且无最新 snapshot
    return {
        "primary_worker": _coordinator._acquired,
        "running": False,
        "scheduler_pid": None,
        "current_pid": os.getpid(),
        "jobs": [],
        "primary_pid": None,
    }


# ============================================================
#  状态快照持久化（DB 共享视图，供所有 worker 读取）
# ============================================================

def _persist_state_snapshot(snapshot: dict) -> None:
    """把 scheduler state 写入一张 key-value 表。

    表由 Alembic 自动创建（如果不存在则 fallback 到 disable）。
    此函数失败不影响主流程，只记录 DEBUG 日志。
    """
    try:
        with SessionLocal() as db:
            # 用 raw SQL UPSERT，避免引入新的 ORM 模型依赖
            from sqlalchemy import text
            db.execute(text("""
                INSERT INTO scheduler_state (id, snapshot, updated_at)
                VALUES ('primary', CAST(:snapshot AS JSONB), NOW())
                ON CONFLICT (id) DO UPDATE
                SET snapshot = EXCLUDED.snapshot,
                    updated_at = NOW()
            """), {"snapshot": _to_json(snapshot)})
            db.commit()
    except Exception:
        # 表可能不存在，记录 DEBUG 即可
        raise


def _read_state_snapshot() -> dict[str, Any] | None:
    """读取最近一次主 worker 写入的 state snapshot。"""
    try:
        with SessionLocal() as db:
            from sqlalchemy import text
            row = db.execute(text("""
                SELECT snapshot, updated_at FROM scheduler_state WHERE id = 'primary'
            """)).first()
            if row is None:
                return None
            snapshot = row[0]
            updated_at = row[1]
            # 数据库返回的 datetime 是 naive datetime，需标记为 UTC 后再比较
            if isinstance(updated_at, datetime) and updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            return {
                "primary_pid": (snapshot.get("primary_pid") if isinstance(snapshot, dict) else None)
                                or (snapshot.get("scheduler_pid") if isinstance(snapshot, dict) else None),
                "running": snapshot.get("running", False) if isinstance(snapshot, dict) else False,
                "jobs": snapshot.get("jobs", []) if isinstance(snapshot, dict) else [],
                "snapshot_at": updated_at if isinstance(updated_at, datetime) else datetime.now(timezone.utc),
            }
    except Exception:
        return None


def _to_json(obj: Any) -> str:
    import json as _json
    return _json.dumps(obj, ensure_ascii=False, default=str)

