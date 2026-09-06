import asyncio, sys
sys.path.insert(0, "/app")
from app.agents.tools import search_project_knowledge
from app.agents.sub_agents import _extract_json
from app.core.db import init_pool, close_pool

async def t():
    await init_pool()
    # 验证知识库检索
    r = await search_project_knowledge("支付系统怎么实现的", 3)
    print("KB results:", len(r.get("results", [])))
    for x in r.get("results", []):
        print("  -", x["heading"], "sim:", x["similarity"])
    # 验证 jloads 的 JSON 提取兼容
    data = _extract_json('{"hotels": [{"name": "测试"}]}')
    print("extract_json OK:", data)
    await close_pool()

asyncio.run(t())
