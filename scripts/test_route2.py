import httpx, asyncio

ports = [10092, 10098]

async def test(port):
    try:
        r = await httpx.AsyncClient().post(f'http://localhost:{port}/api/phone/send-code', json={'phone':'13800138000'})
        print(f'{port} send-code: status={r.status_code}')
    except Exception as e:
        print(f'{port} send-code: ERROR {e}')
        return
    r = await httpx.AsyncClient().get(f'http://localhost:{port}/openapi.json')
    data = r.json()
    paths = list(data['paths'].keys())
    phone = [p for p in paths if 'phone' in p]
    print(f'{port} Total routes: {len(paths)}, Phone: {phone}')

for p in ports:
    asyncio.run(test(p))
