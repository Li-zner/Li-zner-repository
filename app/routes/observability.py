"""可观测性路由：根页面 / favicon / 健康就绪探针 / Prometheus 指标 / OTEL 自测

从 app/main.py 纯移动而来（2026-08-31 模块化，行为等价，零逻辑改动）。
/health 为存活探针、/ready 为就绪探针（供 K8s 滚动分流），均做 PG+Redis 深度依赖检查；
/metrics 供 Prometheus 抓取；/test-otel 用于 OTEL 链路自测（不可用时降级返回）。
"""
from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse, Response

from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

from ..core.db import get_pool
from ..core.redis import get_redis
from ..core.otel import OTEL_AVAILABLE

router = APIRouter()


@router.get("/")
async def root():
    return FileResponse("static/index.html")


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
    except Exception:
        pass  # DB 探测失败即视为不可用（/health /ready 返 503），不向外抛异常
    redis_ok = False
    try:
        r = await get_redis()
        await r.ping()
        redis_ok = True
    except Exception:
        pass  # Redis 探测失败即视为不可用（/health /ready 返 503），不向外抛异常
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
async def prometheus_metrics():
    # 顶部已导入（P2 #15：避免每次抓取重复导入）
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get("/test-otel")
async def test_otel():
    """测试 OTEL 链路（OTEL 不可用时返回提示，不报 500）"""
    if not OTEL_AVAILABLE:
        return {"status": "otel unavailable"}
    from opentelemetry import trace
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("test_span") as span:
        span.set_attribute("test", "hello")
    return {"status": "span created"}
