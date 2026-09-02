"""创建 80 个独占压测账号"""
import asyncio
import asyncpg
import os, sys
from passlib.context import CryptContext

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import db_url

DB_URL = db_url("postgres")


async def main():
    ctx = CryptContext(schemes=["bcrypt_sha256", "bcrypt"])  # C7：新方案走 SHA-256 预处理
    conn = await asyncpg.connect(DB_URL)
    await conn.execute("DELETE FROM users WHERE username LIKE 'load_%'")
    batch = []
    for i in range(1, 81):
        h = ctx.hash("Test@1234", scheme="bcrypt_sha256")
        batch.append((f"load_{i:03d}", h))
    async with conn.transaction():
        for u, h in batch:
            await conn.execute(
                "INSERT INTO users (username, hashed_password, role) VALUES ($1, $2, 'user')",
                u, h,
            )
    cnt = await conn.fetchval("SELECT count(*) FROM users WHERE username LIKE 'load_%'")
    print(f"done: {cnt} accounts (load_001~load_080)")
    await conn.close()


asyncio.run(main())
