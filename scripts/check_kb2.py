import asyncio, sys
sys.path.insert(0, "/app")
from app.agents.tools import search_project_knowledge
from app.core.db import init_pool, close_pool

async def t():
    await init_pool()
    for q in ["支付系统", "项目架构", "技术栈"]:
        r = await search_project_knowledge(q, 3)
        hits = [(x["heading"], x["similarity"]) for x in r.get("results", [])]
        print(q, "->", hits)
    await close_pool()

asyncio.run(t())
