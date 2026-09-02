"""在容器内列出所有用户"""
import asyncio
import asyncpg
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import db_url

DB_URL = db_url("postgres")

async def main():
    conn = await asyncpg.connect(DB_URL)
    rows = await conn.fetch(
        "SELECT username, role, created_at FROM users ORDER BY created_at"
    )
    print(f"共 {len(rows)} 个用户:")
    for r in rows:
        print(f"  {r['username']:<15} role={r['role']:<8} created={r['created_at']}")
    await conn.close()

asyncio.run(main())
