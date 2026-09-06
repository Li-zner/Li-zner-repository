"""创建/重置 admin 用户（密码从 .env 的 ADMIN_PASSWORD 读取）"""
import asyncio
import asyncpg
import os
import sys
from passlib.context import CryptContext

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import get, db_url

async def main():
    ctx = CryptContext(schemes=["bcrypt_sha256", "bcrypt"])  # C7：新方案走 SHA-256 预处理
    dsn = os.getenv("DATABASE_URL") or db_url("postgres")
    conn = await asyncpg.connect(dsn)
    h = ctx.hash(get("ADMIN_PASSWORD"), scheme="bcrypt_sha256")
    await conn.execute("""
        INSERT INTO users (username, hashed_password)
        VALUES ('admin', $1)
        ON CONFLICT (username) DO UPDATE SET hashed_password = EXCLUDED.hashed_password
    """, h)
    row = await conn.fetchrow("SELECT username FROM users WHERE username = $1", "admin")
    print(f"Admin user created: {row}")
    await conn.close()

asyncio.run(main())
