"""测试映射表短回复和超时降级"""
import httpx, json, sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import get

with httpx.Client(timeout=60) as client:
    # Login
    r = client.post('http://localhost:10092/api/login', json={'username':'admin','password':get('ADMIN_PASSWORD')})
    token = r.json()['access_token']
    
    # Test 1: mapping table response (should be short now)
    print("=== 测试1: 映射表回复（七天无理由退货）===")
    resp = client.post('http://localhost:10092/v2/chat/stream',
        headers={'Authorization':f'Bearer {token}','Content-Type':'application/json'},
        json={'query':'网购商品七天无理由退货的法律依据是什么？','persona_id':'civil_code','user':'test_short'},
        timeout=30
    )
    full = ""
    for line in resp.iter_lines():
        if line.startswith('data:'):
            try:
                d = json.loads(line[5:].strip())
                if d.get('type') == 'answer_complete':
                    full = d.get('content', full)
                    break
                elif d.get('type') == 'answer_chunk':
                    full += d.get('content','')
            except:
                pass
    print(f'Length: {len(full)}')
    print(f'Response: {full}')
    
    # Test 2: normal civil code response (should be full)
    print("\n=== 测试2: 正常民法典回复（离婚冷静期）===")
    resp = client.post('http://localhost:10092/v2/chat/stream',
        headers={'Authorization':f'Bearer {token}','Content-Type':'application/json'},
        json={'query':'民法典关于离婚冷静期是怎么规定的？','persona_id':'civil_code','user':'test_full'},
        timeout=30
    )
    full = ""
    for line in resp.iter_lines():
        if line.startswith('data:'):
            try:
                d = json.loads(line[5:].strip())
                if d.get('type') == 'answer_complete':
                    full = d.get('content', full)
                    break
                elif d.get('type') == 'answer_chunk':
                    full += d.get('content','')
            except:
                pass
    print(f'Length: {len(full)}')
    print(f'First 100: {full[:100]}')
