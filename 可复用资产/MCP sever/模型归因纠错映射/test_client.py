"""
验证 Client — correction_map_server（模型归因纠错映射）

流程：握手 → 列工具 → 添加/命中/优先级/miss → 幂等更新 → 清理测试数据。
零外部依赖。注意：会真实写入 data/mappings.json（测试后自动清理测试条目）。
Windows 注意：stdio 依赖命名管道，受限环境（沙箱）会被拒（WinError 5），请在完整权限终端运行。
"""
import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, stdio_client

_SERVER = Path(__file__).resolve().parent / "correction_map_server.py"
_TEST_TRIGGER = "__test_trigger__"


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
            assert {"lookup_redirect", "add_mapping", "list_mappings"} <= set(names)

            # 1. 正常路径：添加 + contains 命中
            r = await session.call_tool(
                "add_mapping", {"trigger": _TEST_TRIGGER, "answer": "测试答案", "mode": "contains"})
            print("添加:", r.content[0].text)
            assert '"added": true' in r.content[0].text

            r = await session.call_tool("lookup_redirect", {"text": f"前缀{_TEST_TRIGGER}后缀"})
            print("contains 命中:", r.content[0].text)
            assert '"hit": true' in r.content[0].text

            # 2. 幂等更新：同 trigger+mode 覆盖
            r = await session.call_tool(
                "add_mapping", {"trigger": _TEST_TRIGGER, "answer": "新测试答案", "mode": "contains"})
            assert '"updated": true' in r.content[0].text
            r = await session.call_tool("lookup_redirect", {"text": _TEST_TRIGGER})
            assert "新测试答案" in r.content[0].text
            print("幂等更新 OK")

            # 3. miss + 边界
            r = await session.call_tool("lookup_redirect", {"text": "完全无关内容xyz"})
            assert '"hit": false' in r.content[0].text
            r = await session.call_tool("lookup_redirect", {"text": ""})
            assert '"error"' in r.content[0].text
            r = await session.call_tool("add_mapping", {"trigger": _TEST_TRIGGER, "answer": "", "mode": "exact"})
            assert '"error"' in r.content[0].text

            # 4. 清理测试数据
            r = await session.call_tool(
                "remove_mapping", {"trigger": _TEST_TRIGGER, "mode": "contains"})
            print("清理:", r.content[0].text)
            assert '"removed": true' in r.content[0].text
            print("边界路径全部通过")


if __name__ == "__main__":
    asyncio.run(main())
    print("验证完成")
