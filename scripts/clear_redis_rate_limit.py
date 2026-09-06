"""清理 Redis 中可能残留的限流键"""
import asyncio
from app.core.redis import get_redis


async def main():
    r = await get_redis()
    keys = await r.keys("qps:*")
    keys += await r.keys("daily_req:*")
    keys += await r.keys("daily_token:*")
    if keys:
        await r.delete(*keys)
        print(f"已清理 {len(keys)} 个限流键")
    else:
        print("无私有限流键")
    await r.aclose()


asyncio.run(main())
