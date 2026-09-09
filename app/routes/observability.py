"""可观测性路由：根页面 / favicon / 健康就绪探针 / Prometheus 指标 / OTEL 自测

从 app/main.py 纯移动而来（2026-08-31 模块化，行为等价，零逻辑改动）。
/health 为存活探针、/ready 为就绪探针（供 K8s 滚动分流），均做 PG+Redis 深度依赖检查；
/metrics 供 Prometheus 抓取；/test-otel 用于 OTEL 链路自测（不可用时降级返回）。
"""
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

from ..core.db import get_pool
from ..core.redis import get_redis
from ..core.otel import OTEL_AVAILABLE

from ..core.logging import setup_logging

logger = setup_logging()

router = APIRouter()


def _check_metrics_token(request: Request) -> JSONResponse | None:
    """/metrics /test-otel 门禁（2026-09-07 审查 P2）：应用层原先完全无鉴权，
    lite 部署靠 nginx 404 兜底，直连容器端口即绕过。

    规则：METRICS_TOKEN 未配置 → fail closed（返回 403 与配置提示，杜绝裸奔）；
    已配置 → 仅接受 Authorization: Bearer <METRICS_TOKEN>（Prometheus 抓取配置
    authorization credentials 即可）。匹配返回 None 放行。
    """
    expected = os.getenv("METRICS_TOKEN", "")
    got = request.headers.get("authorization", "")
    if expected and got == f"Bearer {expected}":
        return None
    return JSONResponse(
        {"detail": "metrics endpoint requires METRICS_TOKEN"
                   "（在网关环境变量配置 METRICS_TOKEN，Prometheus 抓取时携带同值 Bearer Token）"},
        status_code=403,
    )


@router.get("/favicon.ico")
async def favicon():
    """返回最小 SVG 图标避免浏览器 404"""
    return Response(content='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><text y="28" font-size="28">\U0001f30d</text></svg>', media_type="image/svg+xml")


async def _check_deps():
    """检查核心依赖（PG + Redis）；返回 (db_ok, redis_ok)"""
    db_ok = False
    try:
        pool = await get_pool()
        async with pool.acquire(timeout=2) as conn:
            await conn.execute("SELECT 1")
        db_ok = True
    except Exception as e:
        logger.debug(f"DB 探测失败: {e}")  # /health /ready 返 503
    redis_ok = False
    try:
        r = await get_redis()
        await r.ping()
        redis_ok = True
    except Exception as e:
        logger.debug(f"Redis 探测失败: {e}")  # /health /ready 返 503
    return db_ok, redis_ok


@router.get("/health")
async def health():
    """存活探针（Liveness）：深度依赖检查，PG/Redis 任一不可用返回 503（P0 #19）"""
    db_ok, redis_ok = await _check_deps()
    ok = db_ok and redis_ok
    return JSONResponse(
        {"status": "ok" if ok else "degraded", "db": db_ok, "redis": redis_ok},
        status_code=200 if ok else 503,
    )


@router.get("/ready")
async def ready():
    """就绪探针（Readiness）：核心依赖就绪才返回 200，供 K8s 滚动更新分流（A22）"""
    db_ok, redis_ok = await _check_deps()
    ok = db_ok and redis_ok
    return JSONResponse(
        {"status": "ready" if ok else "not_ready", "db": db_ok, "redis": redis_ok},
        status_code=200 if ok else 503,
    )


@router.get("/metrics")
async def prometheus_metrics(request: Request):
    unauthorized = _check_metrics_token(request)
    if unauthorized:
        return unauthorized
    # 顶部已导入（P2 #15：避免每次抓取重复导入）
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get("/test-otel")
async def test_otel(request: Request):
    """测试 OTEL 链路（OTEL 不可用时返回提示，不报 500）；与 /metrics 同门禁"""
    unauthorized = _check_metrics_token(request)
    if unauthorized:
        return unauthorized
    if not OTEL_AVAILABLE:
        return {"status": "otel unavailable"}
    from opentelemetry import trace
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("test_span") as span:
        span.set_attribute("test", "hello")
    return {"status": "span created"}
