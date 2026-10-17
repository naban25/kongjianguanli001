"""FastAPI application factory and ASGI entrypoint."""
from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .core.config import get_settings
from .core.database import check_db_connection
from .core.logging_setup import configure_logging
from .api.v1 import router as api_v1_router
from .services.scheduler import init_scheduler, shutdown_scheduler

configure_logging()
logger = logging.getLogger("app")

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI 生命周期：启动 scheduler、关闭时清理。"""
    settings = get_settings()
    logger.info("App startup — env=%s db=%s", settings.app_env, "configured" if settings.database_url else "missing")

    # 启动调度器。仅持有 fcntl 文件锁的 gunicorn worker 会真的启动 scheduler。
    try:
        sched = init_scheduler()
        if sched is not None:
            logger.info("APScheduler 已在本进程启动，jobs=%d", len(sched.get_jobs()))
        else:
            logger.info("APScheduler 未在本进程启动（其他 worker 已运行 scheduler）")
    except Exception as e:
        logger.exception("启动 scheduler 失败: %s", e)

    yield

    # 关闭 scheduler
    try:
        shutdown_scheduler()
    except Exception as e:
        logger.warning("关闭 scheduler 异常: %s", e)


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="AI Office Lease Management System API",
        docs_url="/docs" if settings.app_env != "production" else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.app_env != "production" else None,
        lifespan=lifespan,
    )

    # CORS — static allowlist + regex for Cloudflare preview domains
    # Starlette 1.x supports allow_origin_regex
    _static_origins = settings.cors_origin_list
    _cf_regex = r"https://([a-zA-Z0-9-]+\.)*(pages|workers)\.dev"

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_static_origins,
        allow_origin_regex=_cf_regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Middleware: request-id + logging
    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request_id_ctx.set(rid)
        request.state.request_id = rid
        logger.info(
            "REQ %s %s (rid=%s)",
            request.method,
            request.url.path,
            rid,
        )
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        logger.info("RES %s %s -> %s (rid=%s)", request.method, request.url.path, response.status_code, rid)
        return response

    # Exception handler (safe, no stack leaks in response)
    @app.exception_handler(Exception)
    async def unhandled_exception(_: Request, exc: Exception):
        logger.exception("Unhandled exception: %s", exc)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "服务器内部错误，请稍后重试或联系管理员。",
                    "request_id": request_id_ctx.get(),
                }
            },
        )

    # ---- Health / Ready endpoints ----
    @app.get("/health", tags=["system"])
    def health() -> dict[str, Any]:
        """进程健康。不做 DB 检查。"""
        return {"status": "ok", "app": settings.app_name, "version": "0.1.0"}

    @app.get("/ready", tags=["system"])
    def ready() -> dict[str, Any]:
        """就绪：进程 + DB。"""
        db_ok, db_msg = check_db_connection()
        llm_ok = settings.llm_configured
        payload = {
            "status": "ok" if db_ok else "degraded",
            "checks": {
                "database": {"ok": db_ok, "detail": None if db_ok else db_msg},
                "llm": {"configured": llm_ok},
            },
        }
        status_code = 200 if db_ok else 503
        return JSONResponse(status_code=status_code, content=payload)

    # ---- API V1 ----
    app.include_router(api_v1_router, prefix="/api/v1")

    @app.get("/", tags=["system"])
    def root() -> dict[str, str]:
        return {"service": settings.app_name, "status": "running"}

    return app


app = create_app()
