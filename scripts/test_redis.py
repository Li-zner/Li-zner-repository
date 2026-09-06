import asyncio, os
os.environ["REDIS_URL"] = "redis://redis:6379/0"

async def test():
    import redis.asyncio as aioredis
    try:
        r = aioredis.from_url("redis://redis:6379/0")
        result = await r.ping()
        print(f"Redis connection: {result}")
        await r.aclose()
    except Exception as e:
        print(f"Redis error: {e}")

asyncio.run(test())
