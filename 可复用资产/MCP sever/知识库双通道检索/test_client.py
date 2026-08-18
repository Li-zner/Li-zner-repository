"""
验证 Client — pg_rag_search_server（知识库双通道检索）

流程：握手 → 列工具 → 真实检索 → 边界（空 query / top_k<=0）→ 参数变体（top_k/sources）。
需真实 PostgreSQL，且已有知识表（默认表 knowledge_chunks，可用 KB_TABLE 覆盖）。
Windows 注意：stdio 依赖命名管道，受限环境（沙箱）会被拒（WinError 5），请在完整权限终端运行。
"""
import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, stdio_client

_SERVER = Path(__file__).resolve().parent / "pg_rag_search_server.py"


async def main():
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(_SERVER)],
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print("发现工具:", names)
            assert "search_knowledge" in names

            # 1. 正常路径：真实查询
            r = await session.call_tool(
                "search_knowledge", {"query": "离婚财产如何分割", "top_k": 3})
            print("检索:", r.content[0].text[:500])
            assert '"total"' in r.content[0].text

            # 2. 参数变体：限定来源 / 小 top_k（query_embedding 缺省则自动跳向量通道）
            r = await session.call_tool(
                "search_knowledge",
                {"query": "结婚年龄", "top_k": 2, "sources": ["civil_code"]},
            )
            print("限定来源检索:", r.content[0].text[:300])

            # 3. 边界路径：空 query / 非法 top_k
            r = await session.call_tool("search_knowledge", {"query": ""})
            assert '"error"' in r.content[0].text
            r = await session.call_tool("search_knowledge", {"query": "x", "top_k": 0})
            assert '"error"' in r.content[0].text
            print("边界路径全部通过")


if __name__ == "__main__":
    asyncio.run(main())
    print("验证完成")
