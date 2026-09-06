import asyncio, sys
sys.path.insert(0, "/app")
from app.agents.router import classify_intent

async def t():
    r = await classify_intent("支付系统怎么实现的", use_llm=False)
    print("agents:", r.get("agents", []))
    print("simple:", r.get("is_simple"))

asyncio.run(t())
