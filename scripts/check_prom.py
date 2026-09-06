import httpx, asyncio

async def test():
    r = await httpx.AsyncClient().get('http://localhost:9090/api/v1/targets')
    data = r.json()
    for t in data['data']['activeTargets']:
        job = t['labels']['job']
        health = t['health']
        addr = t['discoveredLabels']['__address__']
        print(f"  {job:15s} {health:10s} {addr}")

asyncio.run(test())
