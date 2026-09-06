"""检查 load 账号状态"""
import asyncio
import asyncpg
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import db_url

DB_URL = db_url("postgres")


async def check():
    conn = await asyncpg.connect(DB_URL)
    for uid in ["load_062", "load_026", "load_046"]:
        r = await conn.fetchrow("SELECT username FROM users WHERE username=$1", uid)
        print(f"{uid}: {'EXISTS' if r else 'MISSING'}")
    cnt = await conn.fetchval("SELECT COUNT(*) FROM users WHERE username LIKE 'load_%'")
    print(f"Total load accounts: {cnt}")
    await conn.close()


asyncio.run(check())
