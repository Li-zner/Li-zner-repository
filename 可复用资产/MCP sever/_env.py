"""_env.py — 零依赖 .env 读取工具（MCP 资产库共享）

供本目录下所有 MCP Server 复用：进程环境变量 → 本目录 .env → WSL 安全目录。
Server 内用法：
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from _env import get
    key = get("AMAP_API_KEY")

注意：
- 不包含任何真实密钥；先读到的优先（进程环境变量最高）
- 读取用 utf-8-sig：兼容 PowerShell 误写 BOM 的 .env（踩坑清单 #8）
- 安全目录按需改成你自己的项目权威源
"""
import os

_ENV_PATHS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
    # 可按需追加你的权威源，例如 WSL 安全目录：
    # r"\\wsl.localhost\Ubuntu\srv\secrets\.env",
    # "/srv/secrets/.env",
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
    """构造 PostgreSQL 连接串（密码自动 URL 编码，防 # @ 特殊字符炸连接串）"""
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    from urllib.parse import quote_plus
    user = get("DB_USER", "app_user")
    pwd = get("DB_PASSWORD", "")
    dbname = db or get("DB_NAME", "app_db")
    return f"postgresql://{user}:{quote_plus(pwd)}@{host}:5432/{dbname}"
