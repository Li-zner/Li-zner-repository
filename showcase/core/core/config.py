"""
配置管理（Pydantic BaseSettings，2026-08-18 迁移，遗留 A2）

- 统一从环境变量 + .env 读取，类型自动校验（int/float/str），非法值启动即报错。
- 安全凭据缺失 fail loudly（SESSION_SECRET_KEY / JWT_SECRET / ADMIN_PASSWORD）。
- CORS 通配符与 allow_credentials=True 冲突时拒绝启动。
"""
import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class _Settings(BaseSettings):
    """全量配置（字段名小写，自动匹配 UPPER_SNAKE 环境变量，大小写不敏感）"""
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- 数据库 ----------
    database_url: str = ""
    timeout_seconds: float = 30.0

    # ---------- GitHub OAuth ----------
    github_client_id: str = ""
    github_client_secret: str = ""
    session_secret_key: str = ""

    # ---------- JWT ----------
    jwt_secret: str = ""
    jwt_secret_old: str = ""
    access_token_expire_minutes: int = 120
    refresh_token_expire_days: int = 30
    refresh_max_days: int = 30

    # ---------- DeepSeek ----------
    deepseek_api_base: str = "https://api.deepseek.com"
    deepseek_api_timeout: float = 30.0
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_flash_model: str = "deepseek-v4-flash"
    deepseek_fallback_message: str = "服务器繁忙，请稍后再提问吧！"
    rerank_sim_threshold: float = 0.6
    llm_temperature: float = 0.3

    # ---------- 对话管理 ----------
    summary_threshold: int = 20
    history_limit: int = 20
    history_ttl: int = 86400
    history_summary_ttl: int = 604800

    # ---------- 超时 ----------
    tool_timeout: float = 30.0
    http_timeout_short: float = 5.0
    http_timeout_medium: float = 15.0
    http_timeout_long: float = 30.0
    map_api_timeout: float = 8.0
    db_acquire_timeout: float = 10.0
    task_timeout: float = 120.0
    llm_connect_timeout: float = 10.0
    task_ttl_seconds: int = 3600

    # ---------- Embedding / 语义缓存 ----------
    embedding_api_url: str = "http://localhost:11434/api/embeddings"
    embedding_api_key: str = ""
    embedding_model: str = "shaw/dmeta-embedding-zh"
    cache_similarity_threshold: float = 0.85
    cache_l0_ttl: int = 600

    # ---------- 限流（角色分级）----------
    daily_request_limit: int = 50000
    daily_token_limit: int = 10000000
    second_request_limit: int = 500
    admin_qps_limit: int = 5000
    admin_daily_req: int = 1000000
    admin_daily_token: int = 50000000
    admin_concurrent: int = 500
    user_qps_limit: int = 2000
    user_daily_req: int = 500000
    user_daily_token: int = 50000000
    user_concurrent: int = 200

    # ---------- GitHub 试用额度 ----------
    github_question_limit: int = 20

    # ---------- 短信 ----------
    alibaba_cloud_access_key_id: str = ""
    alibaba_cloud_access_key_secret: str = ""
    sms_sign: str = "旅行助手"
    sms_template_code: str = "SMS_000000"
    sms_code_expire_seconds: int = 300
    sms_send_min_interval: int = 60
    sms_send_hour_limit: int = 5
    sms_attempt_limit: int = 5
    sms_attempt_lock_seconds: int = 900

    # ---------- 安全凭据 ----------
    admin_phone: str = ""
    admin_username: str = "admin"
    admin_password: str = ""
    fengfeng_password: str = ""

    # ---------- CORS / 回调 ----------
    cors_origins: str = "http://localhost:10088,http://localhost:10086,http://localhost:10089,http://localhost:10090"
    github_redirect_uri: str = "http://localhost:10088/auth/github/callback"
    gateway_public_url: str = "http://localhost:10088"

    # ---------- 密码 ----------
    bcrypt_max_bytes: int = 72
    # C7：SHA-256 预处理后无 72 字节硬上限，此为策略上限（防超长密码 DoS）
    password_max_bytes: int = 128

    # ---------- 支付 ----------
    payment_success_rate: float = 0.95
    payment_simulated_delay_min: float = 1.0
    payment_simulated_delay_max: float = 3.0
    recharge_min_amount: float = 0.01
    recharge_max_amount: float = 999999.00
    default_wallet_balance: float = 0.00
    order_expire_seconds: int = 600
    token_cost_rate: float = 10.0


_settings = _Settings()

# ===== 安全校验（fail loudly，A2：BaseSettings 迁移后保留）=====
if not _settings.session_secret_key:
    raise RuntimeError("SESSION_SECRET_KEY 环境变量未设置！请在 .env 中配置一个强随机字符串。")
if not _settings.jwt_secret:
    raise RuntimeError("JWT_SECRET 环境变量未设置！请在 .env 中配置一个强随机字符串。")
if not _settings.admin_password and os.getenv("APP_ENV") != "test":
    raise RuntimeError("ADMIN_PASSWORD 环境变量未设置！admin 账户空密码是安全漏洞，请配置强随机字符串。")

# CORS：逗号分隔转列表 + 逐项 trim；通配符与 allow_credentials=True 冲突时拒绝启动
CORS_ORIGINS = [o.strip() for o in _settings.cors_origins.split(",") if o.strip()]
if "*" in CORS_ORIGINS:
    raise RuntimeError("CORS_ORIGINS 不能包含 '*'（与 allow_credentials=True 冲突），请配置具体来源")

# ===== 模块级导出（保持所有 import 名兼容）=====
POSTGRES_DSN = _settings.database_url
DATABASE_URL = POSTGRES_DSN  # MemoryManager 使用的别名
TIMEOUT_SECONDS = _settings.timeout_seconds

GITHUB_CLIENT_ID = _settings.github_client_id
GITHUB_CLIENT_SECRET = _settings.github_client_secret
SESSION_SECRET_KEY = _settings.session_secret_key

SECRET_KEY = _settings.jwt_secret
SECRET_KEY_OLD = _settings.jwt_secret_old or None
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = _settings.access_token_expire_minutes
REFRESH_TOKEN_EXPIRE_DAYS = _settings.refresh_token_expire_days
REFRESH_MAX_DAYS = _settings.refresh_max_days

DEEPSEEK_API_BASE = _settings.deepseek_api_base
DEEPSEEK_API_TIMEOUT = _settings.deepseek_api_timeout
DEEPSEEK_MODEL = _settings.deepseek_model
DEEPSEEK_FLASH_MODEL = _settings.deepseek_flash_model
DEEPSEEK_FALLBACK_MESSAGE = _settings.deepseek_fallback_message
RERANK_SIM_THRESHOLD = _settings.rerank_sim_threshold
LLM_TEMPERATURE = _settings.llm_temperature

SUMMARY_THRESHOLD = _settings.summary_threshold
HISTORY_LIMIT = _settings.history_limit
HISTORY_TTL = _settings.history_ttl
HISTORY_SUMMARY_TTL = _settings.history_summary_ttl

TOOL_TIMEOUT = _settings.tool_timeout
HTTP_TIMEOUT_SHORT = _settings.http_timeout_short
HTTP_TIMEOUT_MEDIUM = _settings.http_timeout_medium
HTTP_TIMEOUT_LONG = _settings.http_timeout_long
MAP_API_TIMEOUT = _settings.map_api_timeout
DB_ACQUIRE_TIMEOUT = _settings.db_acquire_timeout
TASK_TIMEOUT = _settings.task_timeout
LLM_CONNECT_TIMEOUT = _settings.llm_connect_timeout
TASK_TTL_SECONDS = _settings.task_ttl_seconds

EMBEDDING_API_URL = _settings.embedding_api_url
EMBEDDING_API_KEY = _settings.embedding_api_key
EMBEDDING_MODEL = _settings.embedding_model
CACHE_SIMILARITY_THRESHOLD = _settings.cache_similarity_threshold
CACHE_L0_TTL = _settings.cache_l0_ttl

DAILY_REQUEST_LIMIT = _settings.daily_request_limit
DAILY_TOKEN_LIMIT = _settings.daily_token_limit
SECOND_REQUEST_LIMIT = _settings.second_request_limit
ADMIN_QPS_LIMIT = _settings.admin_qps_limit
ADMIN_DAILY_REQ = _settings.admin_daily_req
ADMIN_DAILY_TOKEN = _settings.admin_daily_token
ADMIN_CONCURRENT = _settings.admin_concurrent
USER_QPS_LIMIT = _settings.user_qps_limit
USER_DAILY_REQ = _settings.user_daily_req
USER_DAILY_TOKEN = _settings.user_daily_token
USER_CONCURRENT = _settings.user_concurrent

GITHUB_QUESTION_LIMIT = _settings.github_question_limit

ALIBABA_CLOUD_ACCESS_KEY_ID = _settings.alibaba_cloud_access_key_id.strip()
ALIBABA_CLOUD_ACCESS_KEY_SECRET = _settings.alibaba_cloud_access_key_secret.strip()
SMS_SIGN = _settings.sms_sign
SMS_TEMPLATE_CODE = _settings.sms_template_code
SMS_CODE_EXPIRE_SECONDS = _settings.sms_code_expire_seconds
SMS_SEND_MIN_INTERVAL = _settings.sms_send_min_interval
SMS_SEND_HOUR_LIMIT = _settings.sms_send_hour_limit
SMS_ATTEMPT_LIMIT = _settings.sms_attempt_limit
SMS_ATTEMPT_LOCK_SECONDS = _settings.sms_attempt_lock_seconds

ADMIN_PHONE = _settings.admin_phone
ADMIN_USERNAME = _settings.admin_username
ADMIN_PASSWORD = _settings.admin_password
USER_FENGFENG_PASSWORD = _settings.fengfeng_password

GITHUB_REDIRECT_URI = _settings.github_redirect_uri
GATEWAY_PUBLIC_URL = _settings.gateway_public_url

BCRYPT_MAX_BYTES = _settings.bcrypt_max_bytes  # bcrypt 技术上限（存量截断兼容用，C11 去魔数）
PASSWORD_MAX_BYTES = _settings.password_max_bytes  # C7：新方案密码策略上限（SHA-256 预处理）

PAYMENT_SUCCESS_RATE = _settings.payment_success_rate
PAYMENT_SIMULATED_DELAY_MIN = _settings.payment_simulated_delay_min
PAYMENT_SIMULATED_DELAY_MAX = _settings.payment_simulated_delay_max
RECHARGE_MIN_AMOUNT = _settings.recharge_min_amount
RECHARGE_MAX_AMOUNT = _settings.recharge_max_amount
DEFAULT_WALLET_BALANCE = _settings.default_wallet_balance
ORDER_EXPIRE_SECONDS = _settings.order_expire_seconds
TOKEN_COST_RATE = _settings.token_cost_rate
