import os
import json
import asyncio
import time
import httpx
import secrets
import re
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional, Dict, Any

from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# ---------- OpenTelemetry ----------
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

# ---------- 项目内部导入 ----------
from .core.logging import setup_logging
from .core.config import (
    GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET,
    POSTGRES_DSN, ADMIN_PHONE, ADMIN_USERNAME, ADMIN_PASSWORD,
    USER_FENGFENG_PASSWORD, CORS_ORIGINS, GITHUB_REDIRECT_URI, GATEWAY_PUBLIC_URL,
    HTTP_TIMEOUT_MEDIUM
)
from .core.redis import get_redis
from .core.db import init_pool, close_pool, get_pool
from .core.metrics import gateway_requests_total, gateway_request_duration_seconds
from .middleware.auth import (
    authenticate_user, create_token_pair, revoke_refresh_token,
    get_current_user, get_user, pwd_context, logger, oauth, refresh_access_token,
    create_access_token, create_refresh_token
)
from .middleware.rate_limit import update_daily_usage
from .routes.v2 import router as v2_router
from .routes.map_api import router as map_api_router
from .routes.admin_errors import admin_errors_router
from .payment import router as payment_router
from .payment import admin_router as payment_admin_router
from .cdc.routes import router as cdc_router
from .routes.tenant_routes import router as tenant_admin_router

from prometheus_client import generate_latest, CONTENT_TYPE_LATEST, REGISTRY

logger = setup_logging()

# ============================================================
# 常量（所有魔数集中管理）
# ============================================================
SMS_CODE_EXPIRE_SECONDS = 300          # 验证码有效期 5 分钟
SMS_SEND_COOLDOWN = 60                 # 发送冷却 60 秒
SMS_DAILY_LIMIT = 5                    # 每日最多 5 条
CONCURRENT_LOCK_EXPIRE = 30            # 并发锁过期时间 30 秒
DEFAULT_PASSWORD = "123456789"         # 默认密码
BCRYPT_MAX_BYTES = 72                  # bcrypt 最大有效字节
PHONE_USERNAME_PREFIX = "phone_"       # 手机号用户前缀
MAX_JSON_BODY_SIZE = 1_000_000         # 1MB 请求体限制

# ============================================================
# Pydantic 请求模型（自动校验 + OpenAPI 文档）
# ============================================================
class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=6)

class PhoneSendCodeRequest(BaseModel):
    phone: str = Field(..., pattern=r'^1\d{10}$')

class PhoneRegisterRequest(BaseModel):
    phone: str = Field(..., pattern=r'^1\d{10}$')
    code: str = Field(..., min_length=6, max_length=6)
    password: Optional[str] = Field(None, min_length=6)
    agree: bool = True
    display_name: Optional[str] = None
    email: Optional[str] = None
    extra: Optional[Dict[str, Any]] = {}

class PhoneLoginRequest(BaseModel):
    phone: str = Field(..., pattern=r'^1\d{10}$')
    code: str = Field(..., min_length=6, max_length=6)

class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., min_length=6)
    new_password: str = Field(..., min_length=6)

class BindPhoneRequest(BaseModel):
    phone: str = Field(..., pattern=r'^1\d{10}$')
    code: str = Field(..., min_length=6, max_length=6)

class RateMessageRequest(BaseModel):
    rating: int = Field(..., ge=1, le=5)
    session_id: Optional[str] = None
    user_message: Optional[str] = None
    assistant_message: Optional[str] = None

class SwitchPersonaRequest(BaseModel):
    persona_id: str = Field(..., min_length=1)

# ============================================================
# 数据库初始化（仅建表，不执行 DROP 操作）
# ============================================================
async def _create_core_tables(conn):
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS requests (
            id TEXT PRIMARY KEY,
            start_time REAL,
            first_token_time REAL,
            end_time REAL,
            model_used TEXT,
            status TEXT,
            error TEXT,
            input_tokens INTEGER
        )
    ''')
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS tenants (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            hashed_password TEXT NOT NULL,
            phone TEXT UNIQUE DEFAULT NULL,
            role TEXT DEFAULT 'user',
            display_name TEXT DEFAULT '',
            email TEXT DEFAULT '',
            avatar_url TEXT DEFAULT '',
            extra JSONB DEFAULT '{}',
            quota_limited BOOLEAN NOT NULL DEFAULT FALSE,
            used_requests BIGINT NOT NULL DEFAULT 0,
            tenant_id INTEGER REFERENCES tenants(id),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS user_usage (
            username TEXT,
            date TEXT,
            request_count INTEGER DEFAULT 0,
            token_sum INTEGER DEFAULT 0,
            PRIMARY KEY (username, date)
        )
    ''')
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS conversation_memories (
            id SERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    await conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_conv_memories_lookup
        ON conversation_memories (user_id, conversation_id, created_at DESC)
    ''')
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS user_profiles (
            user_id TEXT PRIMARY KEY,
            profile JSONB DEFAULT '{}',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS message_ratings (
            id BIGSERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            session_id TEXT,
            user_message TEXT,
            assistant_message TEXT,
            rating INT NOT NULL CHECK (rating BETWEEN 1 AND 5),
            created_at TIMESTAMP DEFAULT NOW()
        )
    ''')
    await conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_message_ratings_user
        ON message_ratings (user_id, created_at DESC)
    ''')

async def _ensure_admin_users(conn):
    if ADMIN_PASSWORD:
        row = await conn.fetchrow("SELECT username FROM users WHERE username='admin'")
        if not row:
            hashed = pwd_context.hash(ADMIN_PASSWORD)
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

    fengfeng_pwd = USER_FENGFENG_PASSWORD or ADMIN_PASSWORD
    if fengfeng_pwd:
        row = await conn.fetchrow("SELECT username FROM users WHERE username='Fengfeng'")
        if not row:
            hashed = pwd_context.hash(fengfeng_pwd)
            await conn.execute(
                "INSERT INTO users (username, hashed_password, role) VALUES ($1, $2, 'admin')",
                "Fengfeng", hashed
            )
        else:
            await conn.execute("UPDATE users SET role='admin' WHERE username='Fengfeng'")

async def _create_kb_tables(conn):
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        logger.info("✅ pgvector 扩展已安装")
    except Exception as e:
        logger.warning(f"pgvector 安装失败: {e}")
    try:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_chunks (
                id SERIAL PRIMARY KEY,
                chunk_key TEXT UNIQUE NOT NULL,
                source TEXT NOT NULL DEFAULT 'travel',
                heading TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                embedding vector(768),
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        try:
            await conn.execute("ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'travel'")
        except Exception:
            pass
        logger.info("✅ 知识库表已创建")
    except Exception as e:
        logger.warning(f"知识库表创建失败: {e}")

async def _create_cache_tables(conn):
    await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS semantic_cache (
            id BIGSERIAL PRIMARY KEY,
            query_hash CHAR(64) UNIQUE,
            query_text TEXT,
            response TEXT NOT NULL,
            hit_count INT DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW()
        )
    ''')
    await conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_semantic_trgm ON semantic_cache
        USING gin (query_text gin_trgm_ops)
    ''')

async def init_db(conn=None):
    close_conn = False
    if conn is None:
        pool = await get_pool()
        conn = await pool.acquire()
        close_conn = True
    try:
        await _create_core_tables(conn)
        await _ensure_admin_users(conn)
        await _create_kb_tables(conn)
        try:
            from .payment.service import init_payment_tables
            await init_payment_tables(conn)
            logger.info("✅ 支付相关表已初始化")
        except Exception as e:
            logger.warning(f"支付表初始化失败: {e}")
        await _create_cache_tables(conn)
    finally:
        if close_conn:
            await conn.close()

# ============================================================
# 后台任务管理
# ============================================================
async def _start_background_tasks():
    maintenance_task = None
    try:
        from .core.db_maintenance import maintenance_loop
        maintenance_task = asyncio.create_task(maintenance_loop(24))
        logger.info("✅ 数据库定时维护任务已启动")
    except Exception as e:
        logger.warning(f"维护任务启动跳过: {e}")

    slowq_task = None
    try:
        from .core.slow_query_watch import slow_query_watch_loop
        slowq_task = asyncio.create_task(slow_query_watch_loop())
        logger.info("✅ 慢查询观测已启动")
    except Exception as e:
        logger.warning(f"慢查询观测启动跳过: {e}")

    cdc_task = None
    cdc_worker = None
    try:
        if os.getenv("CDC_ENABLED", "1") == "1":
            from .cdc.worker import CdcWorker
            cdc_worker = CdcWorker()
            cdc_task = asyncio.create_task(cdc_worker.run())
            logger.info("✅ CDC worker 已启动")
    except Exception as e:
        logger.warning(f"CDC worker 启动跳过: {e}")

    return maintenance_task, slowq_task, cdc_task, cdc_worker

async def _warmup_components():
    try:
        from .core.cache_warmup import warmup_semantic_cache
        asyncio.create_task(warmup_semantic_cache())
        logger.info("✅ 语义缓存预热任务已启动")
    except Exception as e:
        logger.warning(f"语义缓存预热跳过: {e}")
    try:
        from .core.safety_filter import get_filter
        get_filter()
        logger.info("✅ DFA 敏感词过滤器已预热")
    except Exception as e:
        logger.warning(f"DFA 预热跳过: {e}")
    try:
        from .core.persona_manager import get_persona_manager
        get_persona_manager()
        logger.info("✅ 人格管理器已预热")
    except Exception as e:
        logger.warning(f"人格预热跳过: {e}")

async def _stop_background_tasks(maintenance_task, slowq_task, cdc_task, cdc_worker):
    if slowq_task:
        slowq_task.cancel()
        try:
            await slowq_task
        except asyncio.CancelledError:
            pass
    if maintenance_task:
        maintenance_task.cancel()
        try:
            await maintenance_task
        except asyncio.CancelledError:
            pass
        logger.info("🛑 数据库维护任务已停止")
    if cdc_task:
        cdc_task.cancel()
        try:
            await cdc_task
        except asyncio.CancelledError:
            pass
    if cdc_worker:
        await cdc_worker.stop()
    if cdc_task:
        logger.info("🛑 CDC worker 已停止")

# ============================================================
# FastAPI 生命周期
# ============================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        pool = await init_pool()
        logger.info("✅ 数据库连接池已初始化")
    except Exception as e:
        logger.warning(f"连接池初始化失败: {e}")

    try:
        await init_db()
        logger.info("✅ PostgreSQL 数据库已初始化")
    except Exception as e:
        logger.warning(f"PostgreSQL 初始化失败: {e}")

    try:
        from .cdc.schema import CDC_EVENTS_TABLE, CDC_EVENTS_INDEX
        async with pool.acquire() as conn:
            await conn.execute(CDC_EVENTS_TABLE)
            await conn.execute(CDC_EVENTS_INDEX)
        logger.info("✅ CDC 事件表已确保")
    except Exception as e:
        logger.warning(f"CDC 事件表初始化跳过: {e}")

    try:
        from .core.db_maintenance import ensure_indexes
        await ensure_indexes()
        logger.info("✅ 数据库索引已确保")
    except Exception as e:
        logger.warning(f"索引创建跳过: {e}")

    try:
        from .core.redis import init_redis
        await init_redis()
        logger.info("✅ Redis 连接池已初始化")
    except Exception as e:
        logger.warning(f"Redis 初始化失败: {e}")

    maintenance_task, slowq_task, cdc_task, cdc_worker = await _start_background_tasks()
    await _warmup_components()

    # OpenTelemetry
    resource = Resource(attributes={SERVICE_NAME: "agent-gateway"})
    provider = TracerProvider(resource=resource)
    processor = BatchSpanProcessor(
        OTLPSpanExporter(endpoint="http://tempo:4318/v1/traces")
    )
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)
    try:
        FastAPIInstrumentor.instrument_app(app)
        logger.info("✅ OpenTelemetry 追踪已启用")
    except Exception as e:
        logger.warning(f"OpenTelemetry 注入失败: {e}")

    yield

    await _stop_background_tasks(maintenance_task, slowq_task, cdc_task, cdc_worker)
    try:
        from .core.redis import close_redis
        await close_redis()
        logger.info("Redis 连接池已关闭")
    except Exception as e:
        logger.warning(f"Redis 关闭失败: {e}")
    try:
        await close_pool()
        logger.info("数据库连接池已关闭")
    except Exception as e:
        logger.warning(f"连接池关闭失败: {e}")

# ============================================================
# FastAPI 应用实例
# ============================================================
app = FastAPI(title="AI Agent Gateway", lifespan=lifespan)

# ============================================================
# 深度健康检查（Redis + PostgreSQL）
# ============================================================
@app.get("/health")
async def health():
    db_ok = False
    redis_ok = False
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("SELECT 1")
        db_ok = True
    except Exception:
        pass
    try:
        r = await get_redis()
        await r.ping()
        redis_ok = True
    except Exception:
        pass
    if db_ok and redis_ok:
        return {"status": "ok", "db": True, "redis": True}
    return JSONResponse(
        status_code=503,
        content={"status": "degraded", "db": db_ok, "redis": redis_ok}
    )

# ============================================================
# 中间件
# ============================================================
@app.middleware("http")
async def monitor_requests(request: Request, call_next):
    start_time = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    except Exception as exc:
        status_code = 500
        try:
            from .core.error_aggregator import record_error
            await record_error(request, exc)
        except Exception:
            pass
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

# CORS（配置校验：["*"] + credentials 不兼容）
if CORS_ORIGINS == ["*"]:
    logger.warning("CORS_ORIGINS=['*'] 与 allow_credentials=True 不兼容，浏览器会拒绝请求")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静态目录（启动前确保存在）
os.makedirs("static", exist_ok=True)
os.makedirs("uploads", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# ============================================================
# 路由注册
# ============================================================
app.include_router(v2_router)
app.include_router(map_api_router, dependencies=[])
app.include_router(admin_errors_router)
app.include_router(payment_router)
app.include_router(payment_admin_router)
app.include_router(cdc_router)
app.include_router(tenant_admin_router)

# ============================================================
# 公共路由
# ============================================================
@app.get("/")
async def root():
    return FileResponse("static/index.html")

@app.get("/favicon.ico")
async def favicon():
    return Response(
        content='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><text y="28" font-size="28">🌍</text></svg>',
        media_type="image/svg+xml"
    )

# --------------------------------------------
# 账号密码登录
# --------------------------------------------
async def resolve_login_username(username: str) -> str:
    """将手机号解析为内部 username（phone_{phone}）"""
    if username.startswith(PHONE_USERNAME_PREFIX) or not re.match(r'^1\d{10}$', username):
        return username
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT username FROM users WHERE phone=$1", username)
        if row:
            return row["username"]
    return username

@app.post("/api/login")
async def login(payload: LoginRequest):
    username = await resolve_login_username(payload.username)
    user = await authenticate_user(username, payload.password)
    if not user:
        raise HTTPException(401, "Invalid username or password")
    pair = create_token_pair(username)
    from .core.audit import audit
    await audit(username, "login", {"method": "password"})
    return {"access_token": pair["access_token"], "refresh_token": pair["refresh_token"], "token_type": "bearer"}

# --------------------------------------------
# Token 刷新 & 登出
# --------------------------------------------
@app.post("/api/refresh")
async def refresh_token(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Missing token")
    token = auth[len("Bearer "):]
    result = await refresh_access_token(token)
    return {
        "access_token": result["access_token"],
        "refresh_token": result["refresh_token"],
        "token_type": "bearer",
    }

@app.post("/api/logout")
async def logout(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Invalid token format")
    await revoke_refresh_token(auth[len("Bearer "):])
    return {"success": True}

# --------------------------------------------
# 手机号验证码（含冷却 + 每日限制）
# --------------------------------------------
@app.post("/api/phone/send-code")
async def send_phone_code(payload: PhoneSendCodeRequest):
    phone = payload.phone
    r = await get_redis()

    cooldown_key = f"phone_cooldown:{phone}"
    if await r.get(cooldown_key):
        raise HTTPException(429, "发送过于频繁，请稍后再试")

    daily_key = f"phone_daily:{phone}:{datetime.now().strftime('%Y%m%d')}"
    daily_count = await r.incr(daily_key)
    if daily_count == 1:
        await r.expire(daily_key, 86400)
    if daily_count > SMS_DAILY_LIMIT:
        raise HTTPException(429, "今日验证码发送次数已达上限")

    code = str(secrets.randbelow(900000) + 100000)
    await r.setex(f"phone_code:{phone}", SMS_CODE_EXPIRE_SECONDS, code)
    await r.setex(cooldown_key, SMS_SEND_COOLDOWN, "1")

    from .core.sms import send_sms
    sent = await send_sms(phone, code)
    if not sent:
        logger.error(f"短信发送失败: phone={phone}")
        raise HTTPException(503, "短信发送失败，请稍后重试")
    return {"message": "验证码已发送", "phone": phone}

# --------------------------------------------
# 手机号注册（事务保护）
# --------------------------------------------
@app.post("/api/phone/register")
async def phone_register(payload: PhoneRegisterRequest):
    phone = payload.phone
    code = payload.code
    password = payload.password or DEFAULT_PASSWORD
    if not payload.agree:
        raise HTTPException(400, "请阅读并同意用户协议")
    if len(password) > BCRYPT_MAX_BYTES:
        raise HTTPException(400, f"密码不能超过 {BCRYPT_MAX_BYTES} 位")

    r = await get_redis()
    stored_code = await r.get(f"phone_code:{phone}")
    if not stored_code:
        raise HTTPException(400, "验证码已过期，请重新发送")
    if stored_code != code:
        raise HTTPException(400, "验证码错误")

    pool = await get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT username FROM users WHERE phone=$1", phone)
        if existing:
            raise HTTPException(400, "该手机号已注册")
        username = f"{PHONE_USERNAME_PREFIX}{phone}"
        hashed = pwd_context.hash(password)

        async with conn.transaction():
            tenant = await conn.fetchrow(
                "INSERT INTO tenants (name) VALUES ($1) RETURNING id",
                f"租户_{username}",
            )
            await conn.execute(
                "INSERT INTO users (username, hashed_password, phone, display_name, email, extra, role, tenant_id) "
                "VALUES ($1, $2, $3, $4, $5, $6, 'user', $7)",
                username, hashed, phone,
                payload.display_name or "", payload.email or "",
                json.dumps(payload.extra or {}), tenant["id"],
            )

    await r.delete(f"phone_code:{phone}")
    pair = create_token_pair(username)
    from .core.audit import audit
    await audit(username, "register", {"phone": phone, "tenant_id": tenant["id"]})
    logger.info(f"手机号注册成功: phone={phone}, username={username}")
    return {
        "access_token": pair["access_token"],
        "refresh_token": pair["refresh_token"],
        "token_type": "bearer",
        "username": username,
        "display_name": payload.display_name or "",
        "is_new": True,
        "default_password_hint": password == DEFAULT_PASSWORD
    }

# --------------------------------------------
# 手机号验证码登录（自动注册 + 事务保护）
# --------------------------------------------
@app.post("/api/phone/login")
async def phone_login(payload: PhoneLoginRequest):
    phone = payload.phone
    code = payload.code

    r = await get_redis()
    stored_code = await r.get(f"phone_code:{phone}")
    if not stored_code:
        raise HTTPException(400, "验证码已过期，请重新发送")
    if stored_code != code:
        raise HTTPException(400, "验证码错误")

    pool = await get_pool()
    is_new = False
    async with pool.acquire() as conn:
        user = await conn.fetchrow("SELECT username FROM users WHERE phone=$1", phone)
        if not user:
            username = f"{PHONE_USERNAME_PREFIX}{phone}"
            hashed = pwd_context.hash(DEFAULT_PASSWORD)
            async with conn.transaction():
                tenant = await conn.fetchrow(
                    "INSERT INTO tenants (name) VALUES ($1) RETURNING id",
                    f"租户_{username}",
                )
                await conn.execute(
                    "INSERT INTO users (username, hashed_password, phone, tenant_id) "
                    "VALUES ($1, $2, $3, $4)",
                    username, hashed, phone, tenant["id"],
                )
            is_new = True
            logger.info(f"手机号自动注册: phone={phone}, username={username}")
        else:
            username = user["username"]

    await r.delete(f"phone_code:{phone}")
    token = create_token_pair(username)
    return {
        "access_token": token["access_token"],
        "refresh_token": token["refresh_token"],
        "token_type": "bearer",
        "username": username,
        "is_new": is_new,
        "default_password_hint": is_new
    }

# --------------------------------------------
# 绑定手机号
# --------------------------------------------
@app.post("/api/user/bind-phone")
async def bind_phone(payload: BindPhoneRequest, current_user: dict[str, Any] = Depends(get_current_user)):
    phone = payload.phone
    code = payload.code

    r = await get_redis()
    stored_code = await r.get(f"phone_code:{phone}")
    if not stored_code:
        raise HTTPException(400, "验证码已过期，请重新发送")
    if stored_code != code:
        raise HTTPException(400, "验证码错误")

    username = current_user["username"]
    pool = await get_pool()
    async with pool.acquire() as conn:
        occupied = await conn.fetchval(
            "SELECT 1 FROM users WHERE phone = $1 AND username <> $2", phone, username
        )
        if occupied:
            raise HTTPException(400, "该手机号已被其他账号绑定")
        result = await conn.execute(
            "UPDATE users SET phone = $1, quota_limited = FALSE, used_requests = 0, "
            "updated_at = CURRENT_TIMESTAMP WHERE username = $2 AND (phone IS NULL OR phone = $1)",
            phone, username,
        )
        if "UPDATE 0" in result:
            raise HTTPException(400, "该账号已绑定其他手机号")

    await r.delete(f"phone_code:{phone}")
    from .core.audit import audit
    await audit(username, "phone_bind", {"phone": phone})
    logger.info(f"手机号绑定成功: username={username}, phone={phone}")
    return {"message": "手机号绑定成功，已解除限制", "quota_limited": False}

# --------------------------------------------
# 用户信息
# --------------------------------------------
@app.get("/api/user/profile")
async def get_user_profile(current_user: dict[str, Any] = Depends(get_current_user)):
    username = current_user["username"]
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT username, phone, role, display_name, email, avatar_url, extra, "
            "quota_limited, used_requests, created_at "
            "FROM users WHERE username=$1", username
        )
        if not row:
            raise HTTPException(404, "用户不存在")
        from .core.quota import remaining_questions
        q_limited = bool(row["quota_limited"]) if row["quota_limited"] is not None else False
        return {
            "username": row["username"],
            "phone": row["phone"] or "",
            "role": row["role"] or "user",
            "display_name": row["display_name"] or "",
            "email": row["email"] or "",
            "avatar_url": row["avatar_url"] or "",
            "extra": row["extra"] if isinstance(row["extra"], dict) else {},
            "quota_limited": q_limited,
            "used_requests": row["used_requests"] or 0,
            "remaining_questions": remaining_questions({
                "role": row["role"] or "user",
                "quota_limited": q_limited,
                "used_requests": row["used_requests"] or 0,
            }),
            "created_at": row["created_at"].isoformat() if row["created_at"] else ""
        }

# --------------------------------------------
# 修改密码（超过 72 位直接拒绝）
# --------------------------------------------
@app.post("/api/user/change-password")
async def change_password(payload: ChangePasswordRequest, current_user: dict[str, Any] = Depends(get_current_user)):
    if len(payload.new_password) > BCRYPT_MAX_BYTES:
        raise HTTPException(400, f"密码长度不能超过 {BCRYPT_MAX_BYTES} 位")
    if payload.new_password == payload.old_password:
        raise HTTPException(400, "新密码不能与旧密码相同")

    username = current_user["username"]
    user = await authenticate_user(username, payload.old_password)
    if not user:
        raise HTTPException(400, "旧密码不正确")

    new_hashed = pwd_context.hash(payload.new_password)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET hashed_password=$1 WHERE username=$2",
            new_hashed, username
        )
    logger.info(f"密码已修改: username={username}")
    return {"message": "密码修改成功"}

# --------------------------------------------
# 消息评分
# --------------------------------------------
@app.post("/api/message/rate")
async def rate_message(payload: RateMessageRequest, current_user: dict[str, Any] = Depends(get_current_user)):
    username = current_user["username"]
    pool = await get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            "SELECT rating FROM message_ratings "
            "WHERE user_id=$1 AND session_id=$2 AND user_message=$3 AND assistant_message=$4 "
            "LIMIT 1",
            username, payload.session_id or "", payload.user_message or "", payload.assistant_message or ""
        )
        if existing is not None:
            return {"message": "已评分，感谢你的评价！", "rating": existing, "already_rated": True}
        await conn.execute(
            "INSERT INTO message_ratings (user_id, session_id, user_message, assistant_message, rating) "
            "VALUES ($1, $2, $3, $4, $5)",
            username, payload.session_id or "", payload.user_message or "", payload.assistant_message or "", payload.rating
        )
    logger.info(f"消息评分: username={username}, rating={payload.rating}")
    return {"message": "感谢你的评价！", "rating": payload.rating}

# --------------------------------------------
# 人格/角色
# --------------------------------------------
@app.get("/api/personas")
async def list_personas():
    from .core.persona_manager import get_persona_manager
    pm = get_persona_manager()
    return {"personas": pm.list_personas(), "current": pm.current_id}

@app.post("/api/persona/switch")
async def switch_persona(payload: SwitchPersonaRequest, current_user: dict[str, Any] = Depends(get_current_user)):
    from .core.persona_manager import get_persona_manager
    pm = get_persona_manager()
    if pm.switch(payload.persona_id):
        return {"message": f"已切换到 {pm.current.name}", "current": pm.current_id}
    raise HTTPException(404, f"人格不存在: {payload.persona_id}")

# --------------------------------------------
# 短信配置诊断（管理员）
# --------------------------------------------
@app.get("/api/sms/check-config")
async def sms_check_config(current_user: dict[str, Any] = Depends(get_current_user)):
    if current_user.get("role") != "admin":
        raise HTTPException(403, "仅管理员可查看")
    from .core.config import (
        ALIBABA_CLOUD_ACCESS_KEY_ID, ALIBABA_CLOUD_ACCESS_KEY_SECRET,
        SMS_SIGN, SMS_TEMPLATE_CODE, ADMIN_PHONE
    )
    key_id = ALIBABA_CLOUD_ACCESS_KEY_ID
    key_id_masked = key_id[:4] + "****" + key_id[-4:] if len(key_id) > 8 else "未配置"
    secret = ALIBABA_CLOUD_ACCESS_KEY_SECRET
    secret_masked = secret[:2] + "****" + secret[-2:] if len(secret) > 4 else "未配置"
    return {
        "access_key_id": key_id_masked,
        "access_key_secret_configured": bool(ALIBABA_CLOUD_ACCESS_KEY_SECRET),
        "sms_sign": SMS_SIGN,
        "sms_template_code": SMS_TEMPLATE_CODE,
        "admin_phone": ADMIN_PHONE,
        "all_configured": bool(ALIBABA_CLOUD_ACCESS_KEY_ID and ALIBABA_CLOUD_ACCESS_KEY_SECRET and SMS_TEMPLATE_CODE != "SMS_000000")
    }

# --------------------------------------------
# GitHub OAuth（UPSERT 消除竞态）
# --------------------------------------------
@app.get("/auth/github")
async def auth_github(request: Request):
    if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET:
        return JSONResponse(
            status_code=503,
            content={"detail": "GitHub 登录暂未配置"}
        )
    return await oauth.github.authorize_redirect(request, GITHUB_REDIRECT_URI)

@app.get("/auth/github/callback")
async def auth_github_callback(request: Request):
    try:
        token = await oauth.github.authorize_access_token(request)
        if not token:
            raise HTTPException(400, "获取 access_token 失败")
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_MEDIUM) as client:
            resp = await client.get(
                'https://api.github.com/user',
                headers={'Authorization': f'Bearer {token["access_token"]}'}
            )
            resp.raise_for_status()
            github_user = resp.json()
            user_email = github_user.get('email')
            if not user_email:
                resp = await client.get(
                    'https://api.github.com/user/emails',
                    headers={'Authorization': f'Bearer {token["access_token"]}'}
                )
                resp.raise_for_status()
                emails = resp.json()
                user_email = next((e['email'] for e in emails if e['primary']), None)
                if not user_email:
                    user_email = github_user['login'] + '@github.com'

        username = github_user['login']
        from .core.audit import audit

        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO users (username, hashed_password, role, display_name, email, avatar_url)
                VALUES ($1, $2, 'user', $3, $4, $5)
                ON CONFLICT (username) DO UPDATE SET
                    email = EXCLUDED.email,
                    avatar_url = EXCLUDED.avatar_url,
                    display_name = EXCLUDED.display_name,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING username, role, email, phone
                """,
                username,
                pwd_context.hash(secrets.token_urlsafe(24)),
                github_user.get('name') or github_user.get('login') or '',
                user_email,
                github_user.get('avatar_url') or '',
            )
            user = dict(row) if row else None

        if not user:
            return RedirectResponse(url=f"{GATEWAY_PUBLIC_URL}/?error=github_user_creation_failed")

        if user.get("role") == "admin" and not user.get("email"):
            return RedirectResponse(url=f"{GATEWAY_PUBLIC_URL}/?error=admin_login_via_github_denied")

        qp = ""
        if user.get("role") != "admin" and not user.get("phone"):
            async with pool.acquire() as conn2:
                await conn2.execute(
                    "UPDATE users SET quota_limited = TRUE WHERE username = $1 AND phone IS NULL",
                    username,
                )
            qp = "&quota_limited=1"

        await audit(username, "login", {"method": "github"})
        access_token = create_access_token({"sub": user["username"]})
        refresh_token = create_refresh_token({"sub": user["username"]})
        redirect_url = f"{GATEWAY_PUBLIC_URL}/?token={access_token}&refresh_token={refresh_token}{qp}"
        return RedirectResponse(url=redirect_url)

    except Exception as e:
        return RedirectResponse(url=f"{GATEWAY_PUBLIC_URL}/?error={str(e)}")

# --------------------------------------------
# 统计（聚合下推数据库）
# --------------------------------------------
@app.get("/api/stats")
async def get_stats(current_user: dict[str, Any] = Depends(get_current_user)):
    pool = await get_pool()
    async with pool.acquire() as conn:
        stats = await conn.fetchrow("""
            SELECT
                COUNT(*) as total,
                AVG(end_time - start_time) as avg_latency,
                AVG(CASE WHEN status='success' THEN 1 ELSE 0 END) as success_rate,
                COUNT(CASE WHEN model_used = 'ollama' THEN 1 END) as fallback_usage
            FROM requests
        """)
        samples = await conn.fetch("""
            SELECT id, model_used, status, start_time, end_time
            FROM requests
            ORDER BY start_time DESC
            LIMIT 5
        """)
    if not stats or stats["total"] == 0:
        return {"total": 0}
    return {
        "total_requests": stats["total"],
        "success_rate": f"{stats['success_rate'] * 100:.1f}%" if stats["success_rate"] is not None else "0%",
        "avg_latency_seconds": round(stats["avg_latency"] or 0, 3),
        "fallback_usage": stats["fallback_usage"] or 0,
        "samples": [dict(s) for s in samples]
    }

# --------------------------------------------
# Prometheus 指标
# --------------------------------------------
@app.get("/metrics")
async def prometheus_metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.get("/test-otel")
async def test_otel():
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("test_span") as span:
        span.set_attribute("test", "hello")
    return {"status": "span created"}

# --------------------------------------------
# 删除对话（事务保护）
# --------------------------------------------
@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    current_user: dict[str, Any] = Depends(get_current_user)
):
    username = current_user["username"]
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                result = await conn.execute(
                    "DELETE FROM conversation_memories "
                    "WHERE user_id = $1 AND conversation_id = $2",
                    username, conversation_id
                )
                deleted_count = result.split()[-1]
                logger.info(f"删除对话消息: user={username}, conv={conversation_id}, deleted={deleted_count}")

                await conn.execute(
                    "DELETE FROM user_profiles WHERE user_id = $1",
                    username
                )
                logger.info(f"已清除用户画像: user={username}")
    except Exception as e:
        logger.error(f"删除对话失败: {e}")
        raise HTTPException(status_code=500, detail=f"删除对话失败: {str(e)}")

    try:
        r = await get_redis()
        await r.delete(f"conv:{conversation_id}")
        logger.info(f"已清除 Redis 缓存: conv:{conversation_id}")
    except Exception as e:
        logger.warning(f"Redis 缓存清除失败: {e}")

    return {"message": "对话已删除", "conversation_id": conversation_id, "profile_cleared": True}

# ============================================================
# 启动入口（reload 由环境变量控制）
# ============================================================
if __name__ == "__main__":
    import uvicorn
    reload = os.getenv("UVICORN_RELOAD", "false").lower() == "true"
    uvicorn.run("app.main:app", host="0.0.0.0", port=10086, reload=reload)