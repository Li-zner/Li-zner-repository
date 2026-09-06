import httpx, asyncio

async def query_prom(qry):
    r = await httpx.AsyncClient().get(f'http://localhost:9090/api/v1/query?query={qry}')
    d = r.json()
    results = d['data']['result']
    if results:
        for res in results[:3]:
            print(f'  {res["metric"]}: {res["value"][1]}')
    else:
        print(f'  (no data)')

async def main():
    print('=== gateway_requests_total ===')
    await query_prom('gateway_requests_total')
    
    print('\n=== up{job="gateway"} ===')
    await query_prom('up{job="gateway"}')
    
    print('\n=== scrape_samples_scraped{job="gateway"} ===')
    await query_prom('scrape_samples_scraped{job="gateway"}')
    
    print('\n=== All gateway-related metrics ===')
    r = await httpx.AsyncClient().get('http://localhost:9090/api/v1/label/__name__/values')
    all_metrics = r.json()['data']
    gw = sorted([m for m in all_metrics if 'gateway' in m.lower()])
    print(f'  {gw}')
    
    print('\n=== tempo_traces_total ===')
    await query_prom('tempo_traces_total')
    
    print('\n=== tempo metrics ===')
    tempo_m = sorted([m for m in all_metrics if 'tempo' in m.lower()])
    print(f'  {tempo_m}')

asyncio.run(main())
