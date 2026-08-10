import os

# ============================================
# 配置区
# ============================================
# 安全：数据库连接串必须来自环境变量（docker-compose 注入），不再内置默认密码。
# 若未配置则置空，连接时 db.py 会给出明确报错（fail loudly），绝不回退到硬编码密钥。
POSTGRES_DSN = os.getenv("DATABASE_URL") or ""
DATABASE_URL = POSTGRES_DSN  # MemoryManager 使用的别名
TIMEOUT_SECONDS = float(os.getenv("TIMEOUT_SECONDS", "30.0"))

# GitHub OAuth 配置
GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET")
SESSION_SECRET_KEY = os.getenv("SESSION_SECRET_KEY")
if not SESSION_SECRET_KEY:
    raise RuntimeError("SESSION_SECRET_KEY 环境变量未设置！请在 .env 中配置一个强随机字符串。")

# JWT 配置（2026-08-11 加固：短时效 + 前端 401 自动刷新）
SECRET_KEY = os.getenv("JWT_SECRET")
if not SECRET_KEY:
    raise RuntimeError("JWT_SECRET 环境变量未设置！请在 .env 中配置一个强随机字符串。")
# 旧密钥（轮换并行期使用；可选，未设置则跳过）。
# 轮换流程: 新 JWT_SECRET 生效后，旧值保留在 JWT_SECRET_OLD，
#           直到所有旧 token 过期再移除（见 scripts/rotate_jwt_secret.sh）。
SECRET_KEY_OLD = os.getenv("JWT_SECRET_OLD") or None
ALGORITHM = "HS256"
# access token 短时效（默认 2 小时；前端 apiFetch 已支持 401 自动刷新）
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "120"))
# 独立 refresh token 有效期（默认 30 天；登出/轮换后旧 refresh 进 Redis 黑名单）
REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "30"))
# 滑动续期最大会话时长：超过该天数未活跃则需重新登录
REFRESH_MAX_DAYS = 30

# DeepSeek 配置
DEEPSEEK_API_BASE = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com")
DEEPSEEK_API_TIMEOUT = float(os.getenv("DEEPSEEK_API_TIMEOUT", "30.0"))
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")          # 主力模型（flash）
DEEPSEEK_FLASH_MODEL = os.getenv("DEEPSEEK_FLASH_MODEL", "deepseek-v4-flash")  # Flash 降级模型
DEEPSEEK_FALLBACK_MESSAGE = os.getenv("DEEPSEEK_FALLBACK_MESSAGE", "服务器繁忙，请稍后再提问吧！")

# 对话管理
SUMMARY_THRESHOLD = int(os.getenv("SUMMARY_THRESHOLD", "20"))

# 工具调用超时
TOOL_TIMEOUT = float(os.getenv("TOOL_TIMEOUT", "30.0"))

# ===== 超时收口（2026-08-11 集中管理，命名 = 用途_语义）=====
# 所有超时统一在此定义，业务代码禁止再写裸数字 timeout=xx
HTTP_TIMEOUT_SHORT   = float(os.getenv("HTTP_TIMEOUT_SHORT", "5.0"))     # 短查询（工具/意图分类）
HTTP_TIMEOUT_MEDIUM  = float(os.getenv("HTTP_TIMEOUT_MEDIUM", "15.0"))   # 中等（编排/子Agent/文件）
HTTP_TIMEOUT_LONG    = float(os.getenv("HTTP_TIMEOUT_LONG", "30.0"))     # 长任务（摘要/兜底）
MAP_API_TIMEOUT      = float(os.getenv("MAP_API_TIMEOUT", "8.0"))        # 地图 API
DB_ACQUIRE_TIMEOUT   = float(os.getenv("DB_ACQUIRE_TIMEOUT", "10.0"))    # 数据库连接获取
TASK_TIMEOUT         = float(os.getenv("TASK_TIMEOUT", "120.0"))         # 任务式聊天整体超时
LLM_CONNECT_TIMEOUT  = float(os.getenv("LLM_CONNECT_TIMEOUT", "10.0"))   # LLM 连接阶段超时
# DEEPSEEK_API_TIMEOUT 在上方 DeepSeek 配置段定义（读超时）


# 语义缓存 & Embedding
EMBEDDING_API_URL = os.getenv("EMBEDDING_API_URL", "http://localhost:11434/api/embeddings")
EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", "")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "shaw/dmeta-embedding-zh")
CACHE_SIMILARITY_THRESHOLD = float(os.getenv("CACHE_SIMILARITY_THRESHOLD", "0.85"))

# 限流配置（求职演示用-大幅放宽）
DAILY_REQUEST_LIMIT = 50000       # 每日请求上限 5万
DAILY_TOKEN_LIMIT = 10000000      # 每日 Token 上限 1000万
SECOND_REQUEST_LIMIT = 500       # 每秒请求上限 500（压测放宽）

# 短信配置（阿里云 SMS）
ALIBABA_CLOUD_ACCESS_KEY_ID = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "").strip(' \"\'')
ALIBABA_CLOUD_ACCESS_KEY_SECRET = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "").strip(' \"\'')
SMS_SIGN = os.getenv("SMS_SIGN", "旅行助手")
SMS_TEMPLATE_CODE = os.getenv("SMS_TEMPLATE_CODE", "SMS_000000")  # 需在阿里云 SMS 控制台申请
# ===== 安全凭据（必须通过环境变量设置，不可使用默认值）=====
ADMIN_PHONE = os.getenv("ADMIN_PHONE", "")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")  # 管理员密码，必须设置
USER_FENGFENG_PASSWORD = os.getenv("FENGFENG_PASSWORD", "")  # Fengfeng用户密码

# CORS 与 OAuth 回调（生产环境必须覆盖）
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "http://localhost:10088,http://localhost:10086,http://localhost:10089,http://localhost:10090").split(",")
GITHUB_REDIRECT_URI = os.getenv("GITHUB_REDIRECT_URI", "http://localhost:10088/auth/github/callback")

# 网关对外地址（用于回调跳转）
GATEWAY_PUBLIC_URL = os.getenv("GATEWAY_PUBLIC_URL", "http://localhost:10088")

# ===== 模拟支付配置 =====
PAYMENT_SUCCESS_RATE = float(os.getenv("PAYMENT_SUCCESS_RATE", "0.95"))          # 模拟支付成功率
PAYMENT_SIMULATED_DELAY_MIN = float(os.getenv("PAYMENT_SIMULATED_DELAY_MIN", "1.0"))  # 最小模拟延迟（秒）
PAYMENT_SIMULATED_DELAY_MAX = float(os.getenv("PAYMENT_SIMULATED_DELAY_MAX", "3.0"))  # 最大模拟延迟（秒）
RECHARGE_MIN_AMOUNT = float(os.getenv("RECHARGE_MIN_AMOUNT", "0.01"))           # 最小充值金额
RECHARGE_MAX_AMOUNT = float(os.getenv("RECHARGE_MAX_AMOUNT", "999999.00"))      # 最大充值金额
DEFAULT_WALLET_BALANCE = float(os.getenv("DEFAULT_WALLET_BALANCE", "0.00"))     # 新用户默认余额（元）
ORDER_EXPIRE_SECONDS = int(os.getenv("ORDER_EXPIRE_SECONDS", "600"))             # 订单过期时间（秒）
TOKEN_COST_RATE = float(os.getenv("TOKEN_COST_RATE", "10.0"))                   # 每万token价格（元）