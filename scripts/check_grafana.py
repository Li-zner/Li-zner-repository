import httpx, asyncio

async def main():
    # Check Grafana data sources
    r = await httpx.AsyncClient().get('http://localhost:3001/api/datasources',
        auth=('admin','admit123'))
    print(f'Grafana data sources ({r.status_code}):')
    for ds in r.json():
        print(f'  {ds["name"]}: type={ds["type"]} url={ds.get("url","")}')
    
    # If no Prometheus datasource, create one
    names = [ds['name'] for ds in r.json()]
    if 'Prometheus' not in names:
        print('\nCreating Prometheus datasource...')
        r2 = await httpx.AsyncClient().post('http://localhost:3001/api/datasources',
            auth=('admin','admit123'),
            json={
                'name': 'Prometheus',
                'type': 'prometheus',
                'url': 'http://prometheus:9090',
                'access': 'proxy',
                'isDefault': True
            })
        print(f'  Result: {r2.status_code} {r2.text[:200]}')
    
    if 'Tempo' not in names:
        print('\nCreating Tempo datasource...')
        r3 = await httpx.AsyncClient().post('http://localhost:3001/api/datasources',
            auth=('admin','admit123'),
            json={
                'name': 'Tempo',
                'type': 'tempo',
                'url': 'http://tempo:3200',
                'access': 'proxy'
            })
        print(f'  Result: {r3.status_code} {r3.text[:200]}')

asyncio.run(main())
