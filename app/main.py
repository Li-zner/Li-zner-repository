import os
import asyncio
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

# ---------- OpenTelemetry (HTTP 导出) ----------
# 保护性导入：OTEL 为可选可观测组件，缺失/环境不兼容时不阻止应用启动（main指点 #4）。
# 可用性真值统一读 app/core/otel.py（唯一权威源），避免多份布尔漂移（2026-08-31）。
from .core.otel import OTEL_AVAILABLE

from .core.logging import setup_logging
from .core.config import (
    SESSION_SECRET_KEY, ADMIN_PHONE, ADMIN_USERNAME, ADMIN_PASSWORD,
    USER_FENGFENG_PASSWORD, CORS_ORIGINS,
)
from .core.db import init_pool, close_pool, get_pool
from .core.metrics import gateway_requests_total, gateway_request_duration_seconds
from .core.password import hash_password
from .core.concurrency import spawn
from .routes.v2 import router as v2_router
from .routes.map_api import router as map_api_router
from .routes.admin_errors import admin_errors_router
from .routes.conversation import router as conversation_router
from .routes.auth import router as auth_router
from .routes.phone import router as phone_router
from .routes.oauth import router as oauth_router
from .routes.users import router as users_router
from .routes.observability import router as observability_router
from .payment import router as payment_router
from .payment import admin_router as payment_admin_router
from .cdc.routes import router as cdc_router

logger = setup_logging()
if not OTEL_AVAILABLE:
    logger.warning("OpenTelemetry 不可用（环境不兼容/缺失），应用降级为无追踪运行")


# ---------- 数据库初始化 ----------

async def _ensure_admin_users(conn):
    """创建/升级管理员账号（凭据通过环境变量传入）"""
    if ADMIN_PASSWORD:
        row = await conn.fetchrow("SELECT username FROM users WHERE username='admin'")
        if not row:
            hashed = hash_password(ADMIN_PASSWORD)
            await conn.execute(
                "INSERT INTO users (username, hashed_password, phone, role, display_name) "
                "VALUES ($1, $2, $3, 'admin', $4)",
                ADMIN_USERNAME, hashed, ADMIN_PHONE or '', "系统管理员"
            )
            logger.info(f"管理员账号已创建: username={ADMIN_USERNAME}")
        else:
            if ADMIN_PHONE:
                await conn.execute(
                    "UPDATE users SET phone=$1, role='admin' WHERE username=$2",
                    ADMIN_PHONE, ADMIN_USERNAME
                )
    else:
        logger.warning("ADMIN_PASSWORD 未设置，跳过管理员创建")

    # 管理员 Fengfeng
    fengfeng_pwd = USER_FENGFENG_PASSWORD or ADMIN_PASSWORD
    if fengfeng_pwd:
        row = await conn.fetchrow("SELECT username FROM users WHERE username='Fengfeng'")
        if not row:
            hashed = hash_password(fengfeng_pwd)
            await conn.execute(
                "INSERT INTO users (username, hashed_password, role) VALUES ($1, $2, 'admin')",
                "Fengfeng", hashed
            )
        else:
            await conn.execute("UPDATE users SET role='admin' WHERE username='Fengfeng'")


async def init_db(conn=None):
    """数据库初始化（A19/A23：DDL 由 Alembic 管理，启动只探活 + 校验表存在）

    GitHub 主流做法：Alembic 是 DDL 唯一入口，应用启动不建表。
    若关键表缺失，fail loudly 提示先执行 alembic upgrade head。
    （Bug #9：原文件此函数整体重复定义了两遍，第二个遮蔽第一个，已去重）
    """
    close_conn = False
    if conn is None:
        pool = await get_pool()
        conn = await pool.acquire()
        close_conn = True
    try:
        await conn.execute("SELECT 1")  # 连接探活
        # 关键表存在性校验（迁移未执行时 fail loudly）
        for _t in ("users", "requests", "conversation_memories", "user_profiles",
                   "semantic_cache", "knowledge_chunks", "payment_orders", "cdc_events"):
            _exists = await conn.fetchval(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = $1", _t)
            if not _exists:
                raise RuntimeError(f"关键表 {_t} 不存在！请先执行数据库迁移：alembic upgrade head")
        # 管理员账号（数据操作，非 DDL）
        await _ensure_admin_users(conn)
    finally:
        if close_conn:
            await conn.close()

async def _start_background_tasks():
    """启动后台任务：维护循环 / 慢查询观测 / CDC worker。返回任务句柄供关闭时使用"""
    maintenance_task = None
    try:
        from .core.db_maintenance import maintenance_loop
        maintenance_task = asyncio.create_task(maintenance_loop(24))
        logger.info("数据库定时维护任务已启动（每24小时）")
    except Exception as e:
        logger.warning(f"维护任务启动跳过: {e}")

    slowq_task = None
    try:
        from .core.slow_query_watch import slow_query_watch_loop
        slowq_task = asyncio.create_task(slow_query_watch_loop())
        logger.info("慢查询观测已启动（每5分钟，pg_stat_statements）")
    except Exception as e:
        logger.warning(f"慢查询观测启动跳过: {e}")

    cdc_task = None
    cdc_worker = None
    try:
        if os.getenv("CDC_ENABLED", "1") == "1":
            from .cdc.worker import CdcWorker
            cdc_worker = CdcWorker()
            cdc_task = asyncio.create_task(cdc_worker.run())
            logger.info("CDC worker 已启动（变更数据捕获落盘）")
    except Exception as e:
        logger.warning(f"CDC worker 启动跳过: {e}")

    return maintenance_task, slowq_task, cdc_task, cdc_worker


async def _warmup_components():
    """预热：语义缓存 / DFA 过滤器 / 人格管理器（避免首个请求冷启动）"""
    try:
        from .core.cache_warmup import warmup_semantic_cache
        # spawn 持强引用：裸 create_task 的后台任务可被 GC 中途回收（2026-09-07 审查 P2）
        spawn(warmup_semantic_cache(), name="cache-warmup")
        logger.info("语义缓存预热任务已启动（后台异步）")
    except Exception as e:
        logger.warning(f"语义缓存预热跳过: {e}")
    try:
        from .core.safety_filter import get_filter
        get_filter()
        logger.info("DFA 敏感词过滤器已预热")
    except Exception as e:
        logger.warning(f"DFA 预热跳过: {e}")
    try:
        from .core.persona_manager import get_persona_manager
        get_persona_manager()
        logger.info("人格管理器已预热")
    except Exception as e:
        logger.warning(f"人格预热跳过: {e}")


async def _stop_background_tasks(maintenance_task, slowq_task, cdc_task, cdc_worker):
    """关闭后台任务（先取消协程，再落 CDC checkpoint）"""
    if slowq_task:
        slowq_task.cancel()
        try:
            await slowq_task
        except asyncio.CancelledError:  # noqa: silent-except 豁免：优雅关停惯例（吞 CancelledError）
            pass
    if maintenance_task:
        maintenance_task.cancel()
        try:
            await maintenance_task
        except asyncio.CancelledError:  # noqa: silent-except 豁免：优雅关停惯例（吞 CancelledError）
            pass
        logger.info("数据库维护任务已停止")
    # 停止 CDC worker（先落 checkpoint 再关文件）
    if cdc_task:
        cdc_task.cancel()
        try:
            await cdc_task
        except asyncio.CancelledError:  # noqa: silent-except 豁免：优雅关停惯例（吞 CancelledError）
            pass
    if cdc_worker:
        await cdc_worker.stop()
    if cdc_task:
        logger.info("CDC worker 已停止")


async def lifespan(app: FastAPI):
    # --- 初始化连接池 ---
    try:
        pool = await init_pool()
        from .core.db import _POOL_CONFIG
        logger.info(f"数据库连接池已初始化 (min={_POOL_CONFIG['min_size']}, max={_POOL_CONFIG['max_size']})")
    except Exception as e:
        logger.warning(f"连接池初始化失败: {e}")

    # --- 初始化数据库表 ---
    try:
        await init_db()
        logger.info("PostgreSQL 数据库已初始化")
    except Exception as e:
        logger.warning(f"PostgreSQL 初始化失败（服务仍可启动，数据库功能受限）: {e}")

    # CDC 事件表与触发器由 Alembic 迁移管理（A19/A23，迁移 e5f6a7b8c9d0），
    # 启动不再执行 DDL；缺失时 init_db 已 fail loudly。
    logger.info("CDC schema 由 Alembic 迁移管理")

    # 初始化 Redis 连接池
    try:
        from .core.redis import init_redis
        await init_redis()
        logger.info("Redis 连接池已初始化")
    except Exception as e:
        logger.warning(f"Redis 初始化失败: {e}")

    # 启动后台任务 + 预热
    maintenance_task, slowq_task, cdc_task, cdc_worker = await _start_background_tasks()
    await _warmup_components()

    # 初始化 OpenTelemetry (HTTP 导出)（保护性：OTEL 不可用时跳过，main指点 #4）
    if OTEL_AVAILABLE:
        try:
            # OTEL 符号仅在可用时懒加载（与旧保护性导入行为一致，见 core/otel.py）
            from .core.otel import (
                Resource, TracerProvider, BatchSpanProcessor, OTLPSpanExporter,
                SERVICE_NAME, trace, FastAPIInstrumentor,
            )
            resource = Resource(attributes={SERVICE_NAME: "agent-gateway"})
            provider = TracerProvider(resource=resource)
            # 端点走环境变量（2026-09-07 审查 P2）：lite 部署无 tempo 时硬编码端点
            # 会让 exporter 后台静默重试；OTEL 不需要时置 OTEL_EXPORTER_OTLP_ENDPOINT 为空跳过
            otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://tempo:4318").rstrip("/")
            if otlp_endpoint:
                processor = BatchSpanProcessor(
                    OTLPSpanExporter(endpoint=f"{otlp_endpoint}/v1/traces")
                )
                provider.add_span_processor(processor)
                trace.set_tracer_provider(provider)
                FastAPIInstrumentor.instrument_app(app)
                logger.info(f"OpenTelemetry 追踪已启用 (OTLP HTTP: {otlp_endpoint})")
        except Exception as e:
            logger.warning(f"OpenTelemetry 初始化失败（不影响路由）: {e}")

    yield
    # --- 关闭时 ---
    await _stop_background_tasks(maintenance_task, slowq_task, cdc_task, cdc_worker)
    # 关闭 Redis
    try:
        from .core.redis import close_redis
        await close_redis()
        logger.info("Redis 连接池已关闭")
    except Exception as e:
        logger.warning(f"Redis 关闭失败: {e}")
    # 关闭数据库连接池
    try:
        await close_pool()
        logger.info("数据库连接池已关闭")
    except Exception as e:
        logger.warning(f"连接池关闭失败: {e}")

app = FastAPI(title="AI Agent Gateway", lifespan=lifespan)

# ---------- 请求监控中间件（Prometheus） ----------
@app.middleware("http")
async def monitor_requests(request: Request, call_next):
    # 请求入口设置 trace_id（P1 #24：一次 OTel 读取，本请求所有日志直接读 contextvar）
    try:
        from .core.logging import set_trace_id
        from opentelemetry import trace
        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.is_valid:
            set_trace_id(format(ctx.trace_id, '032x'))
    except Exception as e:
        logger.debug(f"trace_id 读取失败: {e}")
    start_time = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    except Exception as exc:
        status_code = 500
        # 异常聚合：记录未处理异常（Redis + Prometheus），不阻断主流程
        try:
            from .core.error_aggregator import record_error
            await record_error(request, exc)
        except Exception as e:
            logger.warning("异常聚合写入失败")
        raise
    finally:
        duration = time.perf_counter() - start_time
        route = request.scope.get("route")
        endpoint = route.path if route and hasattr(route, "path") else request.url.path
        gateway_requests_total.labels(
            method=request.method,
            endpoint=endpoint,
            status=str(status_code)
        ).inc()
        gateway_request_duration_seconds.labels(
            method=request.method,
            endpoint=endpoint
        ).observe(duration)

# ---------- 其他中间件 ----------
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# SessionMiddleware 仅用于 GitHub OAuth 的 state 存储（authlib 依赖 request.session），
# 非认证主体（JWT 才是认证）；main指点 #16 建议移除，但移除会破坏 OAuth state 机制，故保留并注明。
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET_KEY)

os.makedirs("uploads", exist_ok=True)
# 安全：uploads 目录不移除通过 StaticFiles 公开挂载，文件只能通过 API 授权访问

# ---------- 路由 ----------
app.include_router(v2_router)
app.include_router(map_api_router, dependencies=[])
app.include_router(admin_errors_router)
app.include_router(payment_router)
app.include_router(payment_admin_router)
app.include_router(cdc_router)
from .routes.tenant_routes import router as tenant_admin_router
app.include_router(tenant_admin_router)
app.include_router(conversation_router)
app.include_router(auth_router)
app.include_router(phone_router)
app.include_router(oauth_router)
app.include_router(users_router)
app.include_router(observability_router)

# ---------- 前端 SPA（frontend/dist，Dockerfile 多阶段构建产出拷入 static/） ----------
# 必须挂在所有 API 路由之后：html=True 让 /chat /login 等 SPA 路径回退到 index.html
# index.html 必须禁缓存：assets hash 随构建变化，缓存旧 html 会引用已删除的旧 hash → 白屏
os.makedirs("static", exist_ok=True)


class _NoCacheStaticFiles(StaticFiles):
    """html 响应加 no-cache 头的 StaticFiles（assets 带 hash 不受影响，浏览器仍可长缓存）"""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        if resp.headers.get("content-type", "").startswith("text/html"):
            resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp


app.mount("/", _NoCacheStaticFiles(directory="static", html=True), name="frontend")

# ============================================================
# 根页面/favicon/健康就绪探针/metrics/OTEL 自测已移至 app/routes/observability.py（2026-08-31 模块化）

# ============================================================
# 用户/评分/人格/统计路由已移至 app/routes/users.py（2026-08-31 模块化）

# ============================================================
# 对话路由已移至 app/routes/conversation.py（2026-08-31 模块化）

if __name__ == "__main__":
    import uvicorn
    # 生产关闭 reload（P0 #9：由环境变量控制，避免文件监控开销与意外重启）
    reload = os.getenv("UVICORN_RELOAD", "false").lower() == "true"
    uvicorn.run("app.main:app", host="0.0.0.0", port=10086, reload=reload)
