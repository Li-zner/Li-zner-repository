"""在容器内执行：向 PostgreSQL 插入测试账号"""
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

    # 清理旧的损坏记录
    await conn.execute(
        "DELETE FROM users WHERE username LIKE 'test%' OR username IN ('demo','guest')"
    )

    accounts = [
        ("test01", "Test@1234"),
        ("test02", "Test@1234"),
        ("test03", "Test@1234"),
        ("test04", "Test@1234"),
        ("test05", "Test@1234"),
        ("test06", "Test@1234"),
        ("test07", "Test@1234"),
        ("test08", "Test@1234"),
        ("test09", "Test@1234"),
        ("test10", "Test@1234"),
        ("test11", "Test@1234"),
        ("test12", "Test@1234"),
        ("test13", "Test@1234"),
        ("test14", "Test@1234"),
        ("test15", "Test@1234"),
        ("demo", "Demo@2026"),
        ("guest", "Guest@2026"),
    ]

    for username, password in accounts:
        h = ctx.hash(password, scheme="bcrypt_sha256")
        await conn.execute(
            "INSERT INTO users (username, hashed_password) VALUES ($1, $2)",
            username,
            h,
        )
        print(f"OK: {username}")

    # 验证
    rows = await conn.fetch("SELECT username, left(hashed_password, 20) FROM users")
    for r in rows:
        print(f"  {r['username']}: {r['left']}...")

    await conn.close()


asyncio.run(main())
