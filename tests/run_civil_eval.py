"""
民法典独立评测运行器
- 只评测 test_cases.yaml 中的 60 条民法典用例
- 从 .env 读取 API Key
"""
import os, sys, json, yaml, asyncio

# 从 .env 加载密钥
env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
if os.path.exists(env_path):
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ[key.strip()] = value.strip()

# 只加载民法典用例
cases_path = os.path.join(os.path.dirname(__file__), "test_cases.yaml")
with open(cases_path, "r", encoding="utf-8") as f:
    raw = yaml.safe_load(f)

all_cases = raw.get("test_cases", raw)
civil_cases_raw = [c for c in all_cases if c.get("category") == "民法典"]
print(f"筛选出 {len(civil_cases_raw)} 个民法典用例（共 {len(all_cases)} 个）")

sys.path.insert(0, os.path.dirname(__file__))
from run_evaluation import TestCase, Evaluator

civil_cases = [TestCase(**c) for c in civil_cases_raw]

config = {
    "gateway_url": "http://localhost:10092",
    "username": "admin",
    "password": os.getenv("ADMIN_PASSWORD", ""),  # 从 .env 读取（文件顶部已加载）
    "llm_api_url": "https://api.deepseek.com/chat/completions",
    "llm_api_key": os.getenv("DEEPSEEK_API_KEY", ""),
    "llm_model": os.getenv("DEEPSEEK_MODEL", "deepseekv4flash"),
    "chat_api_path": "/v2/chat/stream",
    "system_prompt_version": "civil_v2"
}

async def main():
    evaluator = Evaluator(config)
    results = await evaluator.run(civil_cases)

    total = len(results)
    scored = sum(1 for r in results if r.score is not None)
    cached = sum(1 for r in results if r.cached)

    print()
    print("=" * 50)
    print(f"民法典评测完成！")
    print(f"   总用例数: {total}")
    print(f"   已评分: {scored}/{total}")
    print(f"   缓存命中: {cached}/{total}")
    if scored:
        scores = [r.score for r in results if r.score is not None]
        avg = sum(scores) / len(scores)
        high = sum(1 for s in scores if s >= 8)
        mid = sum(1 for s in scores if 6 <= s < 8)
        low = sum(1 for s in scores if s < 6)
        print(f"   平均分: {avg:.2f}")
        print(f"   高分(>=8): {high} | 中等(6-8): {mid} | 低分(<6): {low}")

        low_cases = [(r.case_id, r.score) for r in results if r.score and r.score < 8]
        if low_cases:
            print(f"   低分用例 (<8):")
            for cid, score in sorted(low_cases):
                print(f"     {cid}: {score}/10")

    out_path = "tests/ragas/civil_evaluation_report.xlsx"
    evaluator.export_to_excel(results, out_path)
    print(f"   独立报告: {out_path}")
    
    # 同时保持一份到 tests/ 供RAGAS读取
    main_path = "tests/evaluation_report.xlsx"
    evaluator.export_to_excel(results, main_path)
    print(f"   主报告: {main_path}（供RAGAS读取）")
    print("=" * 50)

if __name__ == "__main__":
    asyncio.run(main())
