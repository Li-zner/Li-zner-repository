"""Direct test of CC001 to see full stream"""
import httpx, json, sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import get

with httpx.Client(timeout=120) as client:
    r = client.post('http://localhost:10092/api/login', json={'username':'admin','password':get('ADMIN_PASSWORD')})
    token = r.json()['access_token']
    
    resp = client.post('http://localhost:10092/v2/chat/stream',
        headers={'Authorization':f'Bearer {token}','Content-Type':'application/json'},
        json={'query':'民法典关于离婚冷静期是怎么规定的？离婚需要什么条件？','persona_id':'civil_code','user':'test_cc001_diag2'},
        timeout=120
    )
    
    events = []
    for line in resp.iter_lines():
        if line.startswith('data:'):
            events.append(line)
            try:
                d = json.loads(line[5:].strip())
                t = d.get('type','')
                if t in ('answer_chunk','answer_complete'):
                    print(f'{t}: {d.get("content","")}')
                elif t == 'tool_call':
                    print(f'tool_call: {d.get("name","")} args={str(d.get("args",{}))[:80]}')
                elif t == 'tool_result':
                    print(f'tool_result (idx={d.get("index","")}): {str(d.get("result",{}))[:80]}')
                elif t == 'reasoning_chunk':
                    print(f'[思考]: {d.get("content","")[:60]}')
                elif t == 'reasoning_done':
                    print(f'[思考完成]')
                elif t == 'thought':
                    print(f'[想法]: {d.get("content","")}')
            except:
                pass
    
    with open(r'D:\桌面\agent_gateway\tests\cc001_stream.txt','w',encoding='utf-8') as f:
        for e in events:
            f.write(e + '\n')
    print(f'\nTotal events: {len(events)}')
    print(f'Saved to cc001_stream.txt')
