"""
MCP 资产库 002 — 高德天气 demo Server（stdio 传输）

演示两个工具：
1. get_server_time  —— 无依赖工具，验证 MCP 协议链路通
2. query_weather    —— 真实工具（高德天气 API），对照项目 app/agents/tools.py 的 fetch_weather_async

运行方式（stdio 模式，供 Client / MCP Inspector 拉起）：
    python weather_server.py

用 FastMCP 高层 API：装饰器声明工具，SDK 自动生成 JSON Schema、处理协议握手。
"""
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

# 复用根目录 _env.py（与 civil-code-rag server 一致：环境变量 → .env → WSL 安全目录）
_ROOT = Path(__file__).resolve().parent.parent.parent.parent  # → 项目根
sys.path.insert(0, str(_ROOT))
from _env import get  # noqa: E402

# 创建 Server 实例（name 会出现在工具列表里）
mcp = FastMCP("agent-gateway-weather-demo")


@mcp.tool()
async def get_server_time(timezone_offset_hours: int = 8) -> str:
    """获取服务器当前时间（默认北京时间 UTC+8）。

    Args:
        timezone_offset_hours: 时区偏移小时数，默认 8（东八区）。
    """
    tz = timezone(timedelta(hours=timezone_offset_hours))
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %z")


@mcp.tool()
async def query_weather(city: str) -> dict:
    """查询指定城市的实时天气（高德地图 API）。

    Args:
        city: 城市名，如 '北京' 或 '上海'。
    """
    amap_key = get("AMAP_API_KEY")
    if not amap_key:
        return {"error": "缺少高德 API Key（AMAP_API_KEY 未配置），请在配置中设置"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # 1. 城市名 → 城市编码（adcode）
            geo_resp = await client.get(
                "https://restapi.amap.com/v3/geocode/geo",
                params={"key": amap_key, "address": city},
            )
            geo_data = geo_resp.json()
            if geo_data["status"] != "1" or not geo_data["geocodes"]:
                return {"error": f"未找到城市: {city}"}
            adcode = geo_data["geocodes"][0]["adcode"]

            # 2. 城市编码 → 实时天气
            weather_resp = await client.get(
                "https://restapi.amap.com/v3/weather/weatherInfo",
                params={"key": amap_key, "city": adcode, "extensions": "base"},
            )
            weather_data = weather_resp.json()
            if weather_data["status"] != "1":
                return {"error": "天气查询失败"}
            live = weather_data["lives"][0]
            return {
                "city": city,
                "temperature": f"{live['temperature']}℃",
                "weather": live["weather"],
                "wind": f"{live['winddirection']}风 {live['windpower']}级",
                "humidity": f"{live['humidity']}%",
            }
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    # stdio 传输：SDK 负责 stdin/stdout 上的 JSON-RPC 协议
    mcp.run()
