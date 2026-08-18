"""
验证 Client — semantic_cache_server（语义缓存）

流程：握手 → 列工具 → store → 三级 lookup（L0 命中/L2 语义命中）→ stats → 边界。
需真实 PostgreSQL（L2 表自动创建）。
Windows 注意：stdio 依赖命名管道，受限环境（沙箱）会被拒（WinError 5），请在完整权限终端运行。
"""
import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, stdio_client

_SERVER = Path(__file__).resolve().parent / "semantic_cache_server.py"


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
            assert {"cache_lookup", "cache_store", "cache_stats"} <= set(names)

            # 1. 写入 + L0 命中（进程内）
            r = await session.call_tool(
                "cache_store", {"query": "离婚财产怎么分", "response": "按共同财产均分"})
            print("写入:", r.content[0].text)
            assert '"stored": true' in r.content[0].text

            r = await session.call_tool("cache_lookup", {"query": "离婚财产怎么分"})
            print("L0 命中:", r.content[0].text)
            assert '"level": "L0"' in r.content[0].text

            # 2. 语义命中（L2，近似问法，阈值内命中）
            r = await session.call_tool(
                "cache_lookup", {"query": "离婚的财产要怎么分呢", "threshold": 0.1})
            print("L2 语义命中:", r.content[0].text)
            assert '"hit": true' in r.content[0].text

            # 3. miss（完全无关问法）
            r = await session.call_tool("cache_lookup", {"query": "今天天气怎么样"})
            print("miss:", r.content[0].text)
            assert '"hit": false' in r.content[0].text

            # 4. 统计与边界
            r = await session.call_tool("cache_stats", {})
            print("统计:", r.content[0].text)
            r = await session.call_tool("cache_lookup", {"query": ""})
            assert '"error"' in r.content[0].text
            print("边界路径全部通过")


if __name__ == "__main__":
    asyncio.run(main())
    print("验证完成")
