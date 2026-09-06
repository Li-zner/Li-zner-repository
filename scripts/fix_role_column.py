"""给 users 表添加 role 列（兼容旧数据库）"""
import asyncio
import asyncpg
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import db_url


async def fix():
    conn = await asyncpg.connect(db_url("postgres"))
    try:
        await conn.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT DEFAULT 'user'"
        )
        print("✅ role 列已添加")
    except Exception as e:
        print(f"添加失败: {e}")
    rows = await conn.fetch("SELECT username, role FROM users LIMIT 5")
    for r in rows:
        print(f"  {r['username']}: {r['role']}")
    await conn.close()


asyncio.run(fix())
