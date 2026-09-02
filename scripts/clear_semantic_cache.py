# -*- coding: utf-8 -*-
"""清语义缓存：PG semantic_cache 表 + Redis 相关键（L0 热缓存）。
在容器内运行（依赖容器自带 DATABASE_URL / REDIS_URL 环境变量）。"""
import asyncio
import os
import asyncpg


async def main():
    # 1) 清 PG 语义缓存表
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        n = await conn.execute("DELETE FROM semantic_cache")
        print("PG semantic_cache cleared:", n)
    finally:
        await conn.close()

    # 2) 清 Redis 缓存相关键（L0 进程内 LRU 需重启容器才清，这里只清 Redis 层）
    try:
        import redis as redis_lib

        r = redis_lib.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"))
        keys = []
        for pattern in ("semantic*", "*semantic_cache*", "*cache*", "kb:*"):
            keys += [k.decode() if isinstance(k, bytes) else k for k in r.keys(pattern)]
        keys = list(set(keys))
        for k in keys:
            r.delete(k)
        print("Redis cache keys cleared:", len(keys), keys[:20])
    except Exception as e:  # noqa: BLE001
        print("Redis clear skipped:", e)


asyncio.run(main())
