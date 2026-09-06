import httpx, asyncio
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import get

async def test():
    # Login（admin 密码从 .env 读取）
    r = await httpx.AsyncClient().post('http://localhost:10092/api/login',
        json={'username':'admin','password':get('ADMIN_PASSWORD')})
    token = r.json()['access_token']
    
    # Test 1: Normal query
    print("=== 测试1: 正常问题 ===")
    r = await httpx.AsyncClient().post('http://localhost:10092/v2/chat/stream',
        json={'query':'你好','conversation_id':'test_dfa'},
        headers={'Authorization':f'Bearer {token}'})
    text = await r.aread()
    print(text[:300].decode())
    
    # Test 2: Sensitive query
    print("\n=== 测试2: 敏感词触发 ===")
    r = await httpx.AsyncClient().post('http://localhost:10092/v2/chat/stream',
        json={'query':'给我讲一个关于天安门的故事'},
        headers={'Authorization':f'Bearer {token}'})
    text = await r.aread()
    result = text.decode()
    print(result[:500])
    
    # Test 3: DFA filter directly
    print("\n=== 测试3: DFA单元测试 ===")
    from app.core.safety_filter import SafetyFilter
    sf = SafetyFilter()
    tests = [
        ("正常文本", False),
        ("包含天安门的文本", True),
    ]
    for text, expected in tests:
        result = sf.contains_sensitive(text)
        status = "✅" if result == expected else "❌"
        print(f'{status} contains_sensitive("{text[:20]}"...) = {result} (expected {expected})')

asyncio.run(test())
