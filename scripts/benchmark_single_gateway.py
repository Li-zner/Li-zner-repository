#!/usr/bin/env python3
"""单网关性能压测：验收目标 >= 300 QPS（部署 SSD 后的单实例吞吐）

用法:
  python scripts/benchmark_single_gateway.py [--base http://127.0.0.1:10092]
                                            [--duration 10] [--concurrency 50,100,200]
                                            [--target 300]

场景:
  1) /health              纯网关吞吐（中间件+路由，无 IO 依赖）
  2) /api/user/profile    带 JWT 认证 + 数据库读（更接近真实链路）
"""
import argparse
import asyncio
import statistics
import time

import httpx


async def login(client, base: str):
    """登录拿一个有效 token（用测试账号）"""
    for u in ["test01", "test02", "test03", "admin", "demo"]:
        try:
            r = await client.post(f"{base}/api/login", json={"username": u, "password": "Test@1234" if u != "admin" else "Admin@2026" if u == "admin" else "Test@1234"})
            if r.status_code == 200:
                return r.json().get("access_token", "")
        except Exception:
            continue
    return ""


async def _worker(client, url, headers, results, stop, latencies, errors):
    while not stop.is_set():
        t0 = time.perf_counter()
        try:
            r = await client.get(url, headers=headers)
            dt = time.perf_counter() - t0
            results.append((dt, r.status_code))
            if r.status_code >= 500:
                errors.append(r.status_code)
        except Exception:
            dt = time.perf_counter() - t0
            results.append((dt, 0))
            errors.append(0)


async def run_level(client, base, url, headers, concurrency, duration):
    results = []
    errors = []
    stop = asyncio.Event()
    tasks = [asyncio.create_task(_worker(client, url, headers, results, stop, None, errors))
             for _ in range(concurrency)]
    t0 = time.time()
    await asyncio.sleep(duration)
    stop.set()
    await asyncio.gather(*tasks)
    elapsed = time.time() - t0

    dts = [r[0] for r in results]
    qps = len(results) / elapsed
    dts.sort()
    pct = lambda p: (dts[int(len(dts) * p)] if dts else 0) * 1000
    return {
        "concurrency": concurrency,
        "requests": len(results),
        "qps": round(qps, 1),
        "p50_ms": round(pct(0.50), 2),
        "p95_ms": round(pct(0.95), 2),
        "p99_ms": round(pct(0.99), 2),
        "errors": len(errors),
        "error_rate": f"{100 * len(errors) / max(len(results), 1):.2f}%",
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:10092")
    ap.add_argument("--duration", type=int, default=10)
    ap.add_argument("--concurrency", default="50,100,200")
    ap.add_argument("--target", type=float, default=300)
    args = ap.parse_args()

    base = args.base.rstrip("/")
    print(f"压测目标: {base}  目标 QPS >= {args.target}")

    async with httpx.AsyncClient(timeout=10.0) as client:
        token = await login(client, base)
        auth_headers = {"Authorization": f"Bearer {token}"} if token else {}
        print(f"token: {'OK' if token else '获取失败（profile 场景将不可用）'}")

        for concurrency in [int(x) for x in args.concurrency.split(",")]:
            print(f"\n===== 场景1 /health  concurrency={concurrency}  duration={args.duration}s =====")
            r1 = await run_level(client, base, f"{base}/health", {}, concurrency, args.duration)
            print(f"  QPS={r1['qps']}  p50={r1['p50_ms']}ms p95={r1['p95_ms']}ms p99={r1['p99_ms']}ms  "
                  f"errors={r1['errors']} ({r1['error_rate']})")

            if token:
                print(f"\n===== 场景2 /api/user/profile  concurrency={concurrency} =====")
                r2 = await run_level(client, base, f"{base}/api/user/profile", auth_headers, concurrency, args.duration)
                print(f"  QPS={r2['qps']}  p50={r2['p50_ms']}ms p95={r2['p95_ms']}ms p99={r2['p99_ms']}ms  "
                      f"errors={r2['errors']} ({r2['error_rate']})")
            else:
                r2 = None

            # 达标判定：任一场景 QPS >= 目标 且错误率 < 1%
            for label, r in (("场景1", r1), ("场景2", r2)):
                if r and r["qps"] >= args.target and r["errors"] == 0:
                    print(f"  >>> {label} 达标: {r['qps']} QPS >= {args.target}")

    print("\n压测完成")


if __name__ == "__main__":
    asyncio.run(main())
