"""
数据库探测脚本 — 验证 RAG MCP Server 的数据链路（不打印任何密钥）

验证三件事：
1. _env.py 能否从 WSL 安全目录读到真实配置
2. PostgreSQL 是否可达（自动尝试 localhost → 局域网 IP，应对 wslrelay 拦截）
3. knowledge_chunks 表是否有民法典数据

用法：
    python mcp_assets/servers/civil-code-rag/db_probe.py
"""
import asyncio
import os
import socket
import sys

# 复用根目录 _env.py（零依赖，从 WSL 安全目录读权威配置）
# db_probe.py → civil-code-rag → servers → mcp_assets → 项目根
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
from _env import get, db_url  # noqa: E402


def _candidate_hosts() -> list:
    """按优先级返回候选数据库主机"""
    hosts = []
    # 1. 进程环境变量显式指定
    if os.getenv("DB_HOST"):
        hosts.append(os.getenv("DB_HOST"))
    # 2. localhost
    hosts.append("localhost")
    # 3. 探测本机监听 5432 的非回环地址（wslrelay 会拦截 localhost:5432）
    try:
        for addr in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = addr[4][0]
            if not ip.startswith("127.") and ip not in hosts:
                hosts.append(ip)
    except Exception:
        pass
    return hosts


async def probe() -> None:
    # 1. 配置读取
    deepseek_key = get("DEEPSEEK_API_KEY", "")
    print(f"[1] DEEPSEEK_API_KEY: {'OK (已从配置读到)' if deepseek_key else 'MISSING'}")
    print(f"[1] DB_USER: {get('DB_USER', 'agent_user')}")

    # 2. 数据库连通性（不打印密码）
    import asyncpg

    for host in _candidate_hosts():
        url = db_url(host)
        # 脱敏显示连接串
        safe = url.replace(url.split("@")[0], "postgresql://***")
        try:
            conn = await asyncpg.connect(url, timeout=5)
            print(f"[2] 连接成功: {safe}")
            # 3. 表数据检查
            row = await conn.fetchrow(
                "SELECT count(*) AS n, count(DISTINCT source) AS sources "
                "FROM knowledge_chunks"
            )
            print(f"[3] knowledge_chunks 行数={row['n']}, 来源数={row['sources']}")
            rows = await conn.fetch(
                "SELECT source, count(*) FROM knowledge_chunks GROUP BY source"
            )
            for r in rows:
                print(f"    source={r['source']}: {r['count']} 行")
            await conn.close()
            print("[OK] 数据链路可用")
            return
        except Exception as e:
            print(f"[2] 失败 {safe}: {type(e).__name__}: {e}")
    print("[FAIL] 所有候选主机均不可达")


if __name__ == "__main__":
    asyncio.run(probe())
