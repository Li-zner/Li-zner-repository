"""
验证 Client — jwt_token_service_server（JWT 令牌签发与校验）

流程：握手 → 列工具 → 签发/验证往返 → 刷新轮换（旧 token 作废）→ 篡改拒绝 → 边界。
无 Redis 也能跑（黑名单走进程内降级）。
Windows 注意：stdio 依赖命名管道，受限环境（沙箱）会被拒（WinError 5），请在完整权限终端运行。
"""
import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, stdio_client

_SERVER = Path(__file__).resolve().parent / "jwt_token_service_server.py"


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
            assert "create_access_token" in names and "verify_token" in names

            # 1. 签发 + 验证往返
            r = await session.call_tool(
                "create_access_token", {"subject": "user_1", "claims": {"role": "admin"}})
            text = r.content[0].text
            print("签发 access:", text[:120])
            assert '"token"' in text
            token = text.split('"token": "')[1].split('"')[0]

            r = await session.call_tool("verify_token", {"token": token})
            print("验证:", r.content[0].text)
            assert '"valid": true' in r.content[0].text

            # 2. 篡改拒绝
            r = await session.call_tool("verify_token", {"token": token[:-4] + "AAAA"})
            print("篡改验证:", r.content[0].text)
            assert '"error"' in r.content[0].text

            # 3. 刷新轮换：旧 refresh 作废
            r = await session.call_tool("create_refresh_token", {"subject": "user_1"})
            refresh = r.content[0].text.split('"token": "')[1].split('"')[0]
            r = await session.call_tool("refresh_access_token", {"refresh_token": refresh})
            print("刷新轮换:", r.content[0].text[:200])
            assert '"access_token"' in r.content[0].text

            # 4. 吊销后拒绝
            r = await session.call_tool("verify_token", {"token": token})
            r = await session.call_tool("revoke_token", {"token": token})
            assert '"revoked": true' in r.content[0].text
            r = await session.call_tool("verify_token", {"token": token})
            print("吊销后验证:", r.content[0].text)
            assert '"error"' in r.content[0].text

            # 5. 边界：空 subject / 空 token
            r = await session.call_tool("create_access_token", {"subject": ""})
            assert '"error"' in r.content[0].text
            r = await session.call_tool("verify_token", {"token": ""})
            assert '"error"' in r.content[0].text
            print("边界路径全部通过")


if __name__ == "__main__":
    asyncio.run(main())
    print("验证完成")
