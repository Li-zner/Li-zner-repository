"""_env.py — 零依赖 .env 读取工具（项目根目录）

用途：让 scripts/、tests/ 及根目录运维脚本从 gitignored 的 .env 读取密钥，
     避免把数据库密码、API Key、admin 密码硬编码进代码库。

使用方式：
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from _env import get, db_url
    api_key = get("DEEPSEEK_API_KEY")
    url = db_url()              # 宿主机脚本默认 localhost
    url = db_url("postgres")    # 容器内脚本用 postgres 主机名

注意：
- 本文件不包含任何真实密钥，全部从 .env 读取
- 读取顺序：根目录 .env（现为占位/开发默认）→ WSL 安全目录 /etc/agent_gateway/.env
- Windows 宿主机经 UNC 路径读取安全目录；WSL/Linux 内直接读 POSIX 路径
- 进程环境变量优先级最高（os.getenv 优先）
"""
import os

# 项目根目录 .env（gitignored，现为占位注释）+ WSL 安全目录 .env（权威源）
_ENV_PATHS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
    r"\\wsl.localhost\Ubuntu\etc\agent_gateway\.env",  # Windows 宿主机（UNC）
    "/etc/agent_gateway/.env",  # WSL / Linux 容器内
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
                    if key and key not in _env:  # 先读到的优先（根目录 .env 优先）
                        _env[key] = value
        except FileNotFoundError:
            continue


_load()


def get(key: str, default: str = "") -> str:
    """优先取进程环境变量，其次取 .env 文件"""
    return os.getenv(key) or _env.get(key, default)


def db_url(host: str = "localhost", db: str = None) -> str:
    """构造数据库连接串（密码自动 URL 编码）

    host: 宿主机脚本用 localhost；容器内脚本用 postgres
    db:   数据库名，默认读取 DB_NAME（缺省 agent_gateway）
    优先级：进程环境变量 DATABASE_URL（如 docker-compose 注入）> 按 host 构造
    """
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    user = get("DB_USER", "agent_user")
    pwd = get("POSTGRES_PASSWORD", "")
    dbname = db or get("DB_NAME", "agent_gateway")
    # URL 编码：密码含 # @ 等特殊字符时必须转义
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
