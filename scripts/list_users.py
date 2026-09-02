import asyncpg, asyncio, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import db_url

async def main():
    pool = await asyncpg.create_pool(db_url("postgres"))
    async with pool.acquire() as c:
        rows = await c.fetch('SELECT username, role FROM users LIMIT 10')
        for r in rows:
            print(r['username'], r['role'])
    await pool.close()

asyncio.run(main())
