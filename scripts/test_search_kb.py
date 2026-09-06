import asyncio, sys
sys.path.insert(0, '/app')
from app.core.db import init_pool, close_pool
from app.agents.tools import search_project_knowledge

async def t():
    await init_pool()
    r = await search_project_knowledge("支付系统", 3)
    print(f"Search results: {len(r.get('results', []))}")
    for x in r.get('results', []):
        print(f"  [{x['heading']}] sim={x['similarity']}")
    r2 = await search_project_knowledge("项目架构", 3)
    print(f"\nSearch2 results: {len(r2.get('results', []))}")
    for x in r2.get('results', []):
        print(f"  [{x['heading']}] sim={x['similarity']}")
    await close_pool()

asyncio.run(t())
