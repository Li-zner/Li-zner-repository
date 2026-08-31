"""测试CC019 - 七天无理由退货"""
import httpx, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import get

with httpx.Client(timeout=60) as client:
    # Login
    login = client.post('http://localhost:10092/api/login', 
                        json={'username': 'admin', 'password': get('ADMIN_PASSWORD')})
    token = login.json()['access_token']
    print('Login OK')
    
    # Test CC019
    payload = {
        'query': '网购商品七天无理由退货的法律依据是什么？哪些商品不适用？',
        'persona_id': 'civil_code',
        'user': 'test_cc019_v2'
    }
    full = ''
    with client.stream('POST', 'http://localhost:10092/v2/chat/stream',
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        json=payload,
        timeout=60
    ) as resp:
        print('Status:', resp.status_code)
        for line in resp.iter_lines():
            # print('LINE:', line[:100])
            if line.startswith('data:'):
                try:
                    d = json.loads(line[5:].strip())
                    if d.get('type') == 'answer_chunk':
                        full += d.get('content', '')
                    elif d.get('type') == 'answer_complete':
                        break
                except:
                    pass
    
    print()
    print('=' * 60)
    print('FULL RESPONSE:')
    print(full[:1000])
    print('=' * 60)
    print()
    print('Contains 消费者权益保护法:', '消费者权益保护法' in full)
    print('Contains 民法典 (as law):', '民法典' in full)
    print('Contains 七天无理由退货:', '七天无理由退货' in full)
    
    # If it mentions civil code first, that's bad
    first_100 = full[:100]
    print()
    print('First 100 chars:', first_100)
    if '民法典' in first_100 and '消费者权益保护法' not in first_100:
        print('❌ BAD: Still attributing to Civil Code first!')
    elif '消费者权益保护法' in first_100 or '消费者权益' in first_100:
        print('✅ GOOD: Correctly identifies Consumer Protection Law!')
