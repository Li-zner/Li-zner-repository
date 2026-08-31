"""本地测试映射表匹配"""
import sys
sys.path.insert(0, r'D:\桌面\agent_gateway')
from app.agents.law_mapping import check_query, reload_mapping, _load_mapping, _calculate_match_score

reload_mapping()
test_q = '网购商品七天无理由退货的法律依据是什么？哪些商品不适用？'
result = check_query(test_q, threshold=0.3)
if result:
    print('MATCH! Score:', result['match_score'])
    print('Law:', result['law'])
    print('Message:', result['message'][:80])
else:
    print('NO MATCH')
    entries = _load_mapping()
    for e in entries[:3]:
        score = _calculate_match_score(test_q, e['keywords'])
        print('  Entry', e['id'], f'({e["scenario"]}):', f'score={score:.2f}', 'keywords=', e['keywords'])

# Also test other queries
for q in ['我被公司辞退了有经济补偿金吗', '离婚冷静期', '小区电梯广告费归谁']:
    r = check_query(q, threshold=0.3)
    if r:
        print(f'\n{q[:20]}... MATCH: {r["law"]} (score={r["match_score"]})')
    else:
        print(f'\n{q[:20]}... NO MATCH')
