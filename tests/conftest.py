"""pytest 全局配置：让测试可离线导入 app 模块

- 把项目根目录加入 sys.path（config.py 依赖 _env.py）
- 设置测试所需的最小环境变量（config.py 对密钥 fail-fast，必须先设置）
"""
import os
import sys

# 项目根目录
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# config.py 对 JWT_SECRET / SESSION_SECRET_KEY / ADMIN_PASSWORD 缺失会直接 raise，
# 正式环境用 APP_ENV != "test" 拒绝空 admin 密码；测试环境须显式置 APP_ENV=test，
# 否则任何 app.* import（config.py 顶层 fail-loudly）都会中断，全部测试文件无法收集。
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-prod")
os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret-not-for-prod")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test_db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
