import asyncio, sys
sys.path.insert(0, '/app')
from app.core.db import init_pool, get_pool, close_pool

async def t():
    await init_pool()
    pool = await get_pool()
    async with pool.acquire() as c:
        r = await c.execute("DELETE FROM semantic_cache")
        print("Cleared:", r)
    await close_pool()

asyncio.run(t())
