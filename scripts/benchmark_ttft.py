#!/usr/bin/env python3
"""首字延迟(TTFT)测量：/v2/chat/stream 流式聊天

测量"请求发出 -> 第一个 answer_chunk 到达"的耗时（首字延迟），
拿到首字后立即断开连接以节省 token。
用法:
  python scripts/benchmark_ttft.py --levels 1,10,25 --per-level 20
"""
import argparse
import asyncio
import json
import random
import time

import httpx

BASE = "http://127.0.0.1:10092"


async def login(client):
    accounts = [
        ("test01", "Test@1234"), ("test02", "Test@1234"), ("test03", "Test@1234"),
        ("demo", "Demo@2026"), ("guest", "Guest@2026"),
    ]
    for u, p in accounts:
        try:
            r = await client.post(f"{BASE}/api/login", json={"username": u, "password": p}, timeout=10)
            if r.status_code == 200:
                return r.json().get("access_token", "")
        except Exception:
            continue
    return ""


async def ttft_one(client, headers, query):
    """返回 (首个事件ttft, 首个answer_chunk ttft)，均可能为 None"""
    t0 = time.perf_counter()
    first_event = None
    first_chunk = None
    try:
        async with client.stream("POST", f"{BASE}/v2/chat/stream",
                                 json={"query": query}, headers=headers, timeout=60) as r:
            if r.status_code != 200:
                return None, None
            async for line in r.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                if first_event is None:
                    first_event = time.perf_counter() - t0
                try:
                    ev = json.loads(data)
                except Exception:
                    continue
                if ev.get("type") == "answer_chunk" and first_chunk is None:
                    first_chunk = time.perf_counter() - t0
                    break  # 拿到首字就断开，省 token
    except Exception:
        pass
    return first_event, first_chunk


async def run_level(client, headers, concurrency, n_req):
    sem = asyncio.Semaphore(concurrency)
    first_event_ttft = []
    first_chunk_ttft = []
    failures = 0

    async def one():
        nonlocal failures
        q = f"你好，请用一句话简单回复（测试{random.randint(1000, 9999)}）"
        async with sem:
            try:
                fe, fc = await ttft_one(client, headers, q)
            except Exception:
                fe = fc = None
        if fe is not None:
            first_event_ttft.append(fe)
        if fc is not None:
            first_chunk_ttft.append(fc)
        if fe is None:
            failures += 1

    tasks = [asyncio.create_task(one()) for _ in range(n_req)]
    await asyncio.gather(*tasks)

    def stats(lst):
        if not lst:
            return None
        lst.sort()
        n = len(lst)
        return {
            "n": n,
            "p50": round(lst[int(n * 0.50)] * 1000, 1),
            "p95": round(lst[int(n * 0.95)] * 1000, 1),
            "p99": round(lst[int(n * 0.99)] * 1000, 1),
            "max": round(lst[-1] * 1000, 1),
        }

    return {
        "concurrency": concurrency,
        "requests": n_req,
        "failures": failures,
        "first_event": stats(first_event_ttft),
        "first_chunk": stats(first_chunk_ttft),
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", default="1,10,25")
    ap.add_argument("--per-level", type=int, default=20)
    args = ap.parse_args()

    async with httpx.AsyncClient(timeout=60) as client:
        token = await login(client)
        if not token:
            print("登录失败")
            return
        headers = {"Authorization": f"Bearer {token}"}
        print(f"首字延迟测量: {BASE}  levels={args.levels}  per-level={args.per_level}")
        print("约束: 首字 < 500ms\n")

        for level in [int(x) for x in args.levels.split(",")]:
            r = await run_level(client, headers, level, args.per_level)
            print(f"===== 并发 {r['concurrency']}  请求 {r['requests']}  失败 {r['failures']} =====")
            fe = r["first_event"]
            fc = r["first_chunk"]
            if fe:
                print(f"  首事件TTFT:  p50={fe['p50']}ms p95={fe['p95']}ms p99={fe['p99']}ms max={fe['max']}ms")
            if fc:
                print(f"  首字TTFT:    p50={fc['p50']}ms p95={fc['p95']}ms p99={fc['p99']}ms max={fc['max']}ms")
                ok = fc["p95"] < 500
                print(f"  >>> 首字 p95<500ms: {'✅ 达标' if ok else '❌ 未达标'} (实际 {fc['p95']}ms)")
            print()


if __name__ == "__main__":
    asyncio.run(main())
