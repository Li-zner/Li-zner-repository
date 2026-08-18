"""全局配置 — 全部来自环境变量，密钥无默认值（缺失即启动失败）
"""
import os

APP_NAME = "app"
ENV = os.getenv("APP_ENV", "dev")

# 密钥：禁止默认值兜底（安全红线）
JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    raise RuntimeError("缺少环境变量 JWT_SECRET")

# 数据库：由 _env 构造或 DATABASE_URL 注入
import sys, os as _os
sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
from _env import get, db_url  # noqa: E402

DATABASE_URL = os.getenv("DATABASE_URL") or db_url("localhost")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# 业务配置（示例）
DEBUG = ENV == "dev"
