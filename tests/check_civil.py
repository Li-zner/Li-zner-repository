"""检查民法典配置和用例"""
import yaml, json

# 1. 检查测试用例结构
print("=== 测试用例结构 ===")
with open("tests/test_cases.yaml", "r", encoding="utf-8") as f:
    cases = yaml.safe_load(f)

all_cases = cases["test_cases"]
print(f"总用例数: {len(all_cases)}")
cats = set(c.get("category", "?") for c in all_cases)
print(f"分类: {cats}")

# 查看第一个用例的结构
if all_cases:
    print(f"用例结构示例: {list(all_cases[0].keys())}")

# 2. 检查民法典人格
print("\n=== 民法典人格 ===")
with open("prompts/civil_code.json", "r", encoding="utf-8") as f:
    p = json.load(f)
print(f"名称: {p['name']}")
print(f"prompt前200字: {p['prompt'][:200]}")
