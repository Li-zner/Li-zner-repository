"""
civil-code-rag MCP Server 验证 Client

验证三大原语：
  Tools    — 正常检索 / 法律误区守卫 / Rerank 开关
  Resources — 知识库覆盖说明 / 法律映射表（可读数据）
  Prompts  — 问题改写模板（可复用提示词）

用法：
    python test_client.py
"""
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def call(session, name: str, args: dict) -> None:
    print(f"[CALL] {name}({json.dumps(args, ensure_ascii=False)})")
    result = await session.call_tool(name, args)
    text = result.content[0].text
    try:
        data = json.loads(text)
        print(json.dumps(data, ensure_ascii=False, indent=2)[:1500])
    except json.JSONDecodeError:
        print(text[:1500])
    print("-" * 60)


async def main() -> None:
    server_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "civil_code_server.py")
    params = StdioServerParameters(command=sys.executable, args=[server_script])

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"[OK] 握手: server={init.serverInfo.name}, protocol={init.protocolVersion}")
            tools = await session.list_tools()
            print(f"[OK] 工具列表: {[t.name for t in tools.tools]}")
            print("=" * 60)

            # ===== Tools（三条路径） =====
            await call(session, "search_civil_code", {"query": "离婚财产怎么分", "top_k": 3})
            await call(session, "search_civil_code", {"query": "买了个手机七天无理由退货被拒绝怎么办", "top_k": 3})
            await call(session, "search_civil_code", {"query": "遗产继承顺序", "top_k": 2, "use_rerank": False})

            # ===== Resources（可读数据） =====
            print("[RESOURCES] 列出资源:")
            resources = await session.list_resources()
            for r in resources.resources:
                print(f"   - {r.uri}: {r.description or ''}")
            print()
            print("[READ] civil://coverage")
            rc = await session.read_resource("civil://coverage")
            print(rc.contents[0].text[:400])
            print("-" * 60)
            print("[READ] civil://law_mapping")
            rm = await session.read_resource("civil://law_mapping")
            print(rm.contents[0].text[:400])
            print("-" * 60)

            # ===== Prompts（可复用模板） =====
            print("[PROMPTS] 列出提示词:")
            prompts = await session.list_prompts()
            for p in prompts.prompts:
                print(f"   - {p.name}: {p.description or ''}")
            print()
            print("[GET] legal_query_rewriter(question='离婚财产怎么分')")
            pr = await session.get_prompt("legal_query_rewriter", {"question": "离婚财产怎么分"})
            for msg in pr.messages:
                print(f"   [{msg.role}] {msg.content.text[:150]}")
            print("-" * 60)


if __name__ == "__main__":
    asyncio.run(main())
