import asyncio, sys
sys.path.insert(0, '/app')
from app.core.db import init_pool, get_pool, close_pool

async def t():
    await init_pool()
    pool = await get_pool()
    async with pool.acquire() as c:
        rows = await c.fetch("SELECT heading FROM knowledge_chunks WHERE source = 'project'")
        print(f"Total: {len(rows)}")
        for r in rows:
            print(f"  {r['heading']}")
    await close_pool()

asyncio.run(t())
