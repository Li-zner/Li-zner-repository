"""添加民法典测试用例到 test_cases.yaml"""
import yaml, copy

CIVIL_CASES = [
    {
        "id": "CC001",
        "category": "民法典",
        "query": "民法典关于离婚冷静期是怎么规定的？",
        "expected_tools": ["search_knowledge"],
        "expected_keywords": ["民法典", "第一千零七十七条", "离婚冷静期", "30日"],
        "difficulty": "简单"
    },
    {
        "id": "CC002",
        "category": "民法典",
        "query": "遗产继承的顺序是什么？第一顺序继承人包括哪些人？",
        "expected_tools": ["search_knowledge"],
        "expected_keywords": ["民法典", "继承", "第一顺序", "配偶", "子女", "父母"],
        "difficulty": "简单"
    },
    {
        "id": "CC003",
        "category": "民法典",
        "query": "租房合同没到期房东要收回房子怎么办？",
        "expected_tools": ["search_knowledge"],
        "expected_keywords": ["民法典", "租赁合同", "违约责任", "第七百零八条"],
        "difficulty": "中等"
    },
    {
        "id": "CC004",
        "category": "民法典",
        "query": "民间借贷的利息最高不能超过多少？",
        "expected_tools": ["search_knowledge"],
        "expected_keywords": ["民法典", "第六百八十条", "利息", "LPR", "四倍"],
        "difficulty": "中等"
    },
    {
        "id": "CC005",
        "category": "民法典",
        "query": "被宠物狗咬伤了，狗主人要负责吗？",
        "expected_tools": ["search_knowledge"],
        "expected_keywords": ["民法典", "侵权责任", "饲养动物", "第一千二百四十五条"],
        "difficulty": "中等"
    },
    {
        "id": "CC006",
        "category": "民法典",
        "query": "购买的商品有质量问题，7天无理由退货的法律依据是什么？",
        "expected_tools": ["search_knowledge"],
        "expected_keywords": ["民法典", "买卖合同", "违约责任", "退货"],
        "difficulty": "中等"
    },
    {
        "id": "CC007",
        "category": "民法典",
        "query": "夫妻共同债务怎么认定？一方借的钱另一方要还吗？",
        "expected_tools": ["search_knowledge"],
        "expected_keywords": ["民法典", "夫妻共同债务", "第一千零六十四条", "共同签名"],
        "difficulty": "中等"
    },
    {
        "id": "CC008",
        "category": "民法典",
        "query": "诉讼时效是多久？过了诉讼时效还能起诉吗？",
        "expected_tools": ["search_knowledge"],
        "expected_keywords": ["民法典", "诉讼时效", "三年", "第一百八十八条"],
        "difficulty": "中等"
    },
    {
        "id": "CC009",
        "category": "民法典",
        "query": "未成年人打赏主播的钱能追回吗？",
        "expected_tools": ["search_knowledge"],
        "expected_keywords": ["民法典", "限制民事行为能力", "未成年人", "打赏", "追回"],
        "difficulty": "中等"
    },
    {
        "id": "CC010",
        "category": "民法典",
        "query": "邻居装修噪音太大影响休息，有没有法律依据可以维权？",
        "expected_tools": ["search_knowledge"],
        "expected_keywords": ["民法典", "相邻关系", "噪音", "侵权", "第二百九十四条"],
        "difficulty": "中等"
    },
    {
        "id": "CC011",
        "category": "民法典",
        "query": "定金和订金有什么区别？交了定金能退吗？",
        "expected_tools": ["search_knowledge"],
        "expected_keywords": ["民法典", "定金", "订金", "第五百八十六条", "定金罚则"],
        "difficulty": "中等"
    },
    {
        "id": "CC012",
        "category": "民法典",
        "query": "离婚时婚前财产怎么分割？婚后买的房子怎么分？",
        "expected_tools": ["search_knowledge"],
        "expected_keywords": ["民法典", "离婚", "财产分割", "婚前财产", "夫妻共同财产"],
        "difficulty": "中等"
    },
]

path = "tests/test_cases.yaml"
with open(path, "r", encoding="utf-8") as f:
    data = yaml.safe_load(f)

# 检查是否已有民法典用例
existing_ids = {c["id"] for c in data["test_cases"]}
new_count = 0
for case in CIVIL_CASES:
    if case["id"] not in existing_ids:
        data["test_cases"].append(case)
        new_count += 1

print(f"新增 {new_count} 条民法典用例")
print(f"总用例数: {len(data['test_cases'])}")

with open(path, "w", encoding="utf-8") as f:
    yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False, width=120)

print("✅ test_cases.yaml 已更新")
