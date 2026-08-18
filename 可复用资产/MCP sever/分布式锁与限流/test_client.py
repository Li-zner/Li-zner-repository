"""
验证 Client — redis_lock_ratelimit_server（分布式锁与限流）

流程：握手 → 列工具 → 真实调用（锁获取/释放/误删保护/限流三件套）→ 边界路径。
需真实 Redis（localhost:6379 或 REDIS_URL）。
Windows 注意：stdio 依赖命名管道，受限环境（沙箱）会被拒（WinError 5），请在完整权限终端运行。
"""
import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, stdio_client

_SERVER = Path(__file__).resolve().parent / "redis_lock_ratelimit_server.py"


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
            assert "acquire_lock" in names and "token_bucket_limit" in names

            # 1. 正常路径：加锁 → 释放
            r = await session.call_tool("acquire_lock", {"key": "test:demo", "ttl": 10})
            result = r.content[0].text
            print("加锁:", result)
            assert '"acquired": true' in result
            token = result.split('"token": "')[1].split('"')[0]

            r = await session.call_tool("release_lock", {"key": "test:demo", "token": token})
            print("释放(本人):", r.content[0].text)
            assert '"released": true' in r.content[0].text

            # 2. 边界路径：错误 token 释放 → 必须失败（Lua 持有者校验）
            r = await session.call_tool("release_lock", {"key": "test:demo", "token": "wrong"})
            print("释放(他人token):", r.content[0].text)
            assert '"released": false' in r.content[0].text

            # 3. 参数变体：固定窗口超限
            for i in range(3):
                r = await session.call_tool(
                    "fixed_window_limit", {"key": "test:qps", "limit": 2, "window_sec": 1})
                print(f"固定窗口第{i+1}次:", r.content[0].text)
            assert '"allowed": false' in r.content[0].text

            # 4. 滑动窗口与令牌桶
            r = await session.call_tool(
                "sliding_window_limit", {"key": "test:sw", "limit": 5, "window_sec": 60})
            print("滑动窗口:", r.content[0].text)
            r = await session.call_tool(
                "token_bucket_limit", {"key": "test:tb", "capacity": 3, "refill_per_sec": 1})
            print("令牌桶:", r.content[0].text)

            # 5. 错误入参：空 key / ttl<=0
            r = await session.call_tool("acquire_lock", {"key": "", "ttl": 5})
            assert '"error"' in r.content[0].text
            r = await session.call_tool("acquire_lock", {"key": "x", "ttl": 0})
            assert '"error"' in r.content[0].text
            print("边界路径全部通过")


if __name__ == "__main__":
    asyncio.run(main())
    print("验证完成")
