"""在容器内创建一个新用户账号"""
import asyncio
import asyncpg
import os, sys
from passlib.context import CryptContext

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import db_url

DB_URL = db_url("postgres")
USERNAME = "user2026"
PASSWORD = "User@2026"

async def main():
    ctx = CryptContext(schemes=["bcrypt_sha256", "bcrypt"])  # C7：新方案走 SHA-256 预处理
    conn = await asyncpg.connect(DB_URL)
    h = ctx.hash(PASSWORD, scheme="bcrypt_sha256")
    await conn.execute(
        "INSERT INTO users (username, hashed_password, role) VALUES ($1, $2, 'user')",
        USERNAME, h,
    )
    row = await conn.fetchrow(
        "SELECT username, role, created_at FROM users WHERE username=$1", USERNAME
    )
    print(f"✅ 创建成功: {row}")
    await conn.close()

asyncio.run(main())
