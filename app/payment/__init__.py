"""模拟支付模块 — 完全模拟真实支付环境"""
from fastapi import APIRouter

router = APIRouter(prefix="/api/payment", tags=["payment"])
admin_router = APIRouter(prefix="/api/admin/payment", tags=["admin_payment"])

from . import routes, admin_routes, service, channels  # noqa: E402, F401
