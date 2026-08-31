"""测试法律映射表"""
import sys, csv
sys.path.insert(0, 'D:\\桌面\\agent_gateway')
from app.agents.law_mapping import check_query, get_all_entries, reload_mapping

# Test loading
entries = reload_mapping()
print(f"Total entries loaded: {len(entries)}")

# Test matching
test_queries = [
    "网购商品七天无理由退货的法律依据是什么？哪些商品不适用？",
    "我被公司辞退了，有经济补偿金吗？",
    "离婚冷静期是怎么规定的？",
    "小区电梯广告费归谁？",
    "我被狗咬伤了，可以要求赔偿吗？",
]

for q in test_queries:
    result = check_query(q, threshold=0.2)
    if result:
        print(f"\n✅ MATCH: {q[:30]}...")
        print(f"   Law: {result['law']}")
        print(f"   Citation: {result['citation']}")
        print(f"   Score: {result['match_score']}")
        print(f"   Message: {result['message'][:80]}...")
    else:
        print(f"\n❌ NO MATCH: {q[:30]}...")
