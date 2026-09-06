import httpx, asyncio

async def test():
    # Test send-code endpoint
    r = await httpx.AsyncClient().post('http://localhost:10092/api/phone/send-code', json={'phone':'13800138000'})
    print(f'send-code: {r.status_code} {r.text[:100]}')
    
    # Check OpenAPI
    r = await httpx.AsyncClient().get('http://localhost:10092/openapi.json')
    data = r.json()
    paths = list(data['paths'].keys())
    phone_paths = [p for p in paths if 'phone' in p]
    print(f'Total routes: {len(paths)}')
    print(f'Phone routes: {phone_paths}')
    
asyncio.run(test())
