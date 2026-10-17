"""API v1 router — all registered endpoints."""
from __future__ import annotations

from fastapi import APIRouter

from .auth import router as auth_router
from .admin import router as admin_router
from .organizations import router as organizations_router
from .spaces import router as spaces_router
from .leases import router as leases_router
from .metrics import router as metrics_router
from .imports import router as imports_router
from .audit import router as audit_router
from .tables import router as tables_router
from .tables_join import router as tables_join_router
from .assistant import router as assistant_router
from .dashboard import router as dashboard_router
from .exports import router as exports_router
from .payments import router as payments_router
from .scheduler_admin import router as scheduler_admin_router

router = APIRouter()


@router.get("/ping")
def ping() -> dict:
    return {"pong": True}


router.include_router(auth_router)
router.include_router(admin_router)
router.include_router(organizations_router)
router.include_router(spaces_router)
router.include_router(leases_router)
router.include_router(metrics_router)
router.include_router(imports_router)
router.include_router(audit_router)
router.include_router(tables_router)
router.include_router(tables_join_router)
router.include_router(assistant_router)
router.include_router(dashboard_router)
router.include_router(exports_router)
router.include_router(payments_router)
router.include_router(scheduler_admin_router)
