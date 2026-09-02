"""从容器内部测试路由"""
import urllib.request, json

# Internal test - bypass port forwarding
try:
    req = urllib.request.Request('http://localhost:10086/openapi.json')
    resp = urllib.request.urlopen(req, timeout=5)
    data = json.loads(resp.read())
    paths = list(data['paths'].keys())
    phone_paths = [p for p in paths if 'phone' in p]
    print(f'Internal test - Total routes: {len(paths)}')
    print(f'Internal test - Phone routes: {phone_paths}')
except Exception as e:
    print(f'Error: {e}')
