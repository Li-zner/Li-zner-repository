"""migrate.py — 数据库迁移助手（Alembic 封装）

解决 Windows + Docker Desktop (WSL2) 环境的特殊问题：
- localhost:5432 会被 wslrelay.exe 拦截导致认证失败
- postgres 容器 IP 从 Windows 宿主不可达（WSL2 桥接网络限制）
- 可靠路径：局域网 IP（命中 com.docker.backend 的 0.0.0.0:5432 监听）

用法（在项目根目录执行）：
    python scripts/migrate.py upgrade head      # 应用全部迁移
    python scripts/migrate.py downgrade -1      # 回退一个版本
    python scripts/migrate.py revision -m "msg" # 新建迁移（自动生成）
    python scripts/migrate.py stamp head        # 标记当前库为某版本（已有表时用）
    python scripts/migrate.py current           # 查看当前版本
    python scripts/migrate.py history           # 查看迁移历史

主机解析优先级：环境变量 DB_HOST > localhost(可用时) > 局域网 IP(自动探测)
"""
import asyncio
import os
import socket
import sys

import asyncpg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import get, db_url  # noqa: E402


def _lan_ips() -> list:
    """探测本机非回环 IPv4 地址"""
    ips = set()
    try:
        # gethostbyname_ex 直接返回本机全部 IPv4，比 getaddrinfo 更可靠
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127."):
                ips.add(ip)
    except Exception:
        pass
    return sorted(ips)


def _can_connect(host: str, timeout: float = 3.0) -> bool:
    """测试能否用当前凭据连上数据库"""
    from urllib.parse import quote_plus
    pwd = quote_plus(get("POSTGRES_PASSWORD", ""))
    url = f"postgresql://{get('DB_USER', 'agent_user')}:{pwd}@{host}:5432/{get('DB_NAME', 'agent_gateway')}"

    async def _probe():
        try:
            conn = await asyncio.wait_for(
                asyncpg.connect(url, timeout=timeout), timeout=timeout + 2
            )
            await conn.close()
            return True
        except Exception:
            return False

    try:
        return asyncio.run(_probe())
    except Exception:
        return False


def resolve_host() -> str:
    """返回一个可用的数据库主机名"""
    explicit = os.getenv("DB_HOST")
    if explicit:
        print(f"[migrate] 使用 DB_HOST={explicit}")
        return explicit
    if _can_connect("localhost"):
        print("[migrate] localhost 连接可用")
        return "localhost"
    for ip in _lan_ips():
        if _can_connect(ip):
            print(f"[migrate] localhost 不可用（wslrelay 拦截），改用局域网 IP {ip}")
            return ip
    raise SystemExit("无法连接数据库：请检查 postgres 容器是否运行，或设置 DB_HOST 环境变量")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    cmd = sys.argv[1]
    args = sys.argv[2:]

    host = resolve_host()
    # 让 alembic/env.py 通过环境变量拿到可用的连接串
    os.environ["DATABASE_URL"] = db_url(host)

    import alembic.command
    import alembic.config

    cfg = alembic.config.Config("alembic.ini")

    if cmd == "upgrade":
        alembic.command.upgrade(cfg, args[0] if args else "head")
    elif cmd == "downgrade":
        alembic.command.downgrade(cfg, args[0] if args else "-1")
    elif cmd == "revision":
        kwargs = {}
        if "--autogenerate" in args:
            kwargs["autogenerate"] = True
            args = [a for a in args if a != "--autogenerate"]
        if args:
            kwargs["message"] = args[0]
        alembic.command.revision(cfg, **kwargs)
    elif cmd == "stamp":
        alembic.command.stamp(cfg, args[0] if args else "head")
    elif cmd == "current":
        alembic.command.current(cfg)
    elif cmd == "history":
        alembic.command.history(cfg)
    else:
        print(f"未知命令: {cmd}\n")
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
