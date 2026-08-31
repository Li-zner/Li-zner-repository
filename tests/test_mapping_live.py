"""测试法律映射表是否生效"""
import httpx, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import get

with httpx.Client(timeout=60) as client:
    # Login
    login = client.post('http://localhost:10092/api/login',
                        json={'username': 'admin', 'password': get('ADMIN_PASSWORD')})
    token = login.json()['access_token']
    print('Login OK')

    test_cases = [
        ("CC019 - 七天无理由退货", "网购商品七天无理由退货的法律依据是什么？哪些商品不适用？"),
        ("CC004 - 劳动合同解除", "我被公司辞退了，有经济补偿金吗？"),
        ("CC009 - 离婚冷静期（应该匹配民法典）", "离婚冷静期是怎么规定的？离婚需要什么条件？"),
        ("CC023 - 被狗咬伤（应该匹配民法典）", "被狗咬伤了找谁赔？狗主人要负全责吗？"),
        ("小区广告费", "小区电梯广告费归谁？业主还是物业？"),
    ]

    for label, query in test_cases:
        print(f'\n{"="*60}')
        print(f'测试: {label}')
        print(f'查询: {query}')
        
        full = ''
        with client.stream('POST', 'http://localhost:10092/v2/chat/stream',
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
            json={'query': query, 'persona_id': 'civil_code', 'user': 'test_mapping'},
            timeout=60
        ) as resp:
            has_mapping = False
            for line in resp.iter_lines():
                if line.startswith('data:'):
                    try:
                        d = json.loads(line[5:].strip())
                        if d.get('type') == 'answer_chunk':
                            full += d.get('content', '')
                        elif d.get('type') == 'answer_complete':
                            break
                    except:
                        pass
        
        print(f'回复: {full[:200]}...')
        if '不属于《中华人民共和国民法典》' in full:
            print('✅ 命中映射表 - 正确引导到其他法律')
        elif '民法典' in full[:100]:
            print('❌ 未命中映射表 - 仍在使用民法典回答')
        else:
            print('⚠️ 其他回复')
