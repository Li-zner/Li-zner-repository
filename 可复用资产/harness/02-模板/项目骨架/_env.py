"""_env.py — 零依赖 .env 读取工具（项目根目录）

用途：让 scripts/、tests/ 及运维脚本从安全目录读取密钥，禁止硬编码。

使用方式：
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from _env import get, db_url
    api_key = get("API_KEY")
    url = db_url()              # 宿主机脚本默认 localhost
    url = db_url("db")          # 容器内脚本用服务名

注意：
- 本文件不包含任何真实密钥，全部从 .env / 安全目录读取
- 读取顺序：根目录 .env（占位/开发默认）→ 安全目录（权威源）
- Windows 宿主机经 UNC 读取安全目录；WSL/Linux 内直接读 POSIX 路径
- 进程环境变量优先级最高（os.getenv 优先）
"""
import os

# 项目根目录 .env（占位）+ 安全目录 .env（权威源，按项目改路径）
_ENV_PATHS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
    # Windows 宿主机（UNC；项目名替换 <project>）
    r"\\wsl.localhost\Ubuntu\etc\<project>\.env",
    # WSL / Linux 容器内
    "/etc/<project>/.env",
]

_env = {}


def _load():
    for path in _ENV_PATHS:
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key and key not in _env:  # 先读到的优先
                        _env[key] = value
        except FileNotFoundError:
            continue


_load()


def get(key: str, default: str = "") -> str:
    """优先取进程环境变量，其次取 .env 文件"""
    return os.getenv(key) or _env.get(key, default)


def db_url(host: str = "localhost", db: str = None) -> str:
    """构造数据库连接串（密码自动 URL 编码）

    host: 宿主机脚本用 localhost；容器内脚本用服务名
    优先级：进程环境变量 DATABASE_URL > 按 host 构造
    """
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    user = get("DB_USER", "app_user")
    pwd = get("DB_PASSWORD", "")
    dbname = db or get("DB_NAME", "app_db")
    from urllib.parse import quote_plus
    return f"postgresql://{user}:{quote_plus(pwd)}@{host}:5432/{dbname}"


def load_env_file(path: str) -> None:
    """显式加载指定 .env 文件（供特殊场景使用）"""
    global _env
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in _env:
                    _env[key] = value
    except FileNotFoundError:
        pass
