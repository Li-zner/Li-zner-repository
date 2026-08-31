"""Direct test CC051 to diagnose truncation"""
import httpx, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import get

with httpx.Client(timeout=120) as client:
    r = client.post('http://localhost:10092/api/login', json={'username':'admin','password':get('ADMIN_PASSWORD')})
    token = r.json()['access_token']
    
    resp = client.post('http://localhost:10092/v2/chat/stream',
        headers={'Authorization':f'Bearer {token}','Content-Type':'application/json'},
        json={'query':'民法典关于居住权的设立和消灭条件是什么？居住权对二手房交易有什么影响？','persona_id':'civil_code','user':'test_cc051_v2'},
        timeout=120
    )
    
    events = []
    full = ""
    for line in resp.iter_lines():
        if line.startswith('data:'):
            events.append(line[:150])
            try:
                d = json.loads(line[5:].strip())
                t = d.get('type','')
                if t == 'answer_chunk':
                    full += d.get('content','')
                elif t == 'answer_complete':
                    full = d.get('content', full)
                    break
            except:
                pass
    
    print(f'Total events: {len(events)}')
    print(f'Response length: {len(full)}')
    print(f'Response: {full[:500]}')
    
    # Save raw events
    with open(r'D:\桌面\agent_gateway\tests\cc051_raw.txt','w',encoding='utf-8') as f:
        f.write('\n'.join(events))
