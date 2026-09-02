"""
MCP 学习 Lab — MCP Client（stdio 传输）

作用：以 stdio 方式拉起 weather_server.py 子进程，完成 MCP 握手，
列出 Server 暴露的工具，并实际调用两个工具，验证整条链路。

运行方式：
    python quick_client.py
"""
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> None:
    # 1. 定义如何拉起 Server 子进程（stdio 传输）
    #    注意：args 用绝对路径，避免子进程 cwd 与脚本位置不一致
    server_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "weather_server.py")
    server_params = StdioServerParameters(
        command=sys.executable,   # 用当前解释器（已装 mcp 1.26）
        args=[server_script],
        # env 可传环境变量；不传则继承父进程
    )

    # 2. 建立会话：stdio_client 启动子进程并连接管道
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            # 3. 协议握手（initialize）
            init = await session.initialize()
            print(f"[OK] 握手成功: server={init.serverInfo.name}, "
                  f"protocol={init.protocolVersion}")
            print()

            # 4. 列出 Server 暴露的工具（对应第二课的工具清单）
            tools = await session.list_tools()
            print(f"[TOOLS] Server 暴露 {len(tools.tools)} 个工具:")
            for t in tools.tools:
                print(f"   - {t.name}: {t.description.splitlines()[0] if t.description else ''}")
            print()

            # 5. 调用无依赖工具：get_server_time
            print("[CALL] get_server_time(timezone_offset_hours=8)")
            r1 = await session.call_tool("get_server_time", {"timezone_offset_hours": 8})
            print(f"   => {r1.content[0].text}")
            print()

            # 6. 调用真实工具：query_weather（无 AMAP key 时返回友好错误，同样验证协议）
            print("[CALL] query_weather(city='北京')")
            r2 = await session.call_tool("query_weather", {"city": "北京"})
            # 工具返回的是文本块，解析其中的 JSON
            text = r2.content[0].text
            try:
                data = json.loads(text)
                print(f"   => {json.dumps(data, ensure_ascii=False, indent=2)}")
            except json.JSONDecodeError:
                print(f"   => {text}")


if __name__ == "__main__":
    asyncio.run(main())
