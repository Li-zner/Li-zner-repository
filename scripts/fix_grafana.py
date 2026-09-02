"""Fix Grafana datasources and test"""
import httpx, asyncio

GURL = 'http://localhost:3001'
AUTH = ('admin', 'admit123')

async def main():
    # List datasources
    r = await httpx.AsyncClient().get(f'{GURL}/api/datasources', auth=AUTH)
    dss = r.json()
    print('Data sources:')
    for ds in dss:
        print(f'  {ds["name"]}: type={ds["type"]} uid={ds["uid"]} isDefault={ds.get("isDefault")}')
    
    # Find prometheus uid
    for ds in dss:
        if 'prometheus' in ds['name'].lower() and ds.get('isDefault'):
            puid = ds['uid']
            print(f'\nDefault Prometheus UID: {puid}')
    
    # Test Prometheus query via Grafana
    for ds in dss:
        if 'prometheus' in ds['name'].lower():
            print(f'\nTesting datasource: {ds["name"]} (uid={ds["uid"]})')
            r2 = await httpx.AsyncClient().post(f'{GURL}/api/ds/query',
                auth=AUTH,
                json={
                    "queries": [{
                        "datasource": {"type": "prometheus", "uid": ds["uid"]},
                        "expr": "gateway_requests_total{job='gateway'}",
                        "refId": "A"
                    }]
                })
            if r2.status_code == 200:
                results = r2.json().get('results', {}).get('A', {}).get('frames', [])
                if results:
                    print(f'  ✅ Data found! {len(results)} frames')
                else:
                    print(f'  ⚠️ Query succeeded but no data frames')
            else:
                print(f'  ❌ Error: {r2.status_code} {r2.text[:200]}')

asyncio.run(main())
