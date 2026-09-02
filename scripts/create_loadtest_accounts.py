"""创建 500 个独立压测账号（loadtest_001 ~ loadtest_500）"""
import asyncio
import asyncpg
import os
import sys
from passlib.context import CryptContext

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import db_url

PASSWORD = "LoadTest@2026"
COUNT = 500


async def main():
    ctx = CryptContext(schemes=["bcrypt_sha256", "bcrypt"])  # C7：新方案走 SHA-256 预处理
    dsn = os.getenv("DATABASE_URL") or db_url("postgres")
    conn = await asyncpg.connect(dsn)

    # 清理旧的 loadtest 账号
    await conn.execute("DELETE FROM users WHERE username LIKE 'loadtest_%'")
    print(f"🧹 已清理旧 loadtest 账号")

    # 批量插入
    batch = []
    for i in range(1, COUNT + 1):
        username = f"loadtest_{i:03d}"
        hashed = ctx.hash(PASSWORD, scheme="bcrypt_sha256")
        batch.append((username, hashed))

    async with conn.transaction():
        for username, hashed in batch:
            await conn.execute(
                "INSERT INTO users (username, hashed_password, role) VALUES ($1, $2, 'user')",
                username,
                hashed,
            )

    print(f"✅ 已创建 {COUNT} 个压测账号 (loadtest_001 ~ loadtest_{COUNT:03d})")
    print(f"📌 密码: {PASSWORD}")

    # 验证
    row = await conn.fetchrow("SELECT COUNT(*) FROM users WHERE username LIKE 'loadtest_%'")
    print(f"📊 数据库验证: {row['count']} 个账号")

    await conn.close()


asyncio.run(main())
