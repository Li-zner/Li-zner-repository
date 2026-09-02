"""
RAGAS 评估脚本
================
评估方式：
1. 从 evaluation_report.xlsx 读取用户问题 + Agent回答
2. 从 test_cases.yaml 读取 expected_keywords 作为 ground_truth
3. 使用 DeepSeek API 作为 LLM 进行 RAGAS 评估
4. 输出评估结果到 ragas_report.xlsx

独立文件，不修改任何已有代码（可插拔设计）。
"""

import os
import sys
import json
import asyncio
from pathlib import Path
from typing import List, Dict, Optional

# 确保能找到项目模块
sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter
import httpx


# ============================================================
# 配置
# ============================================================
EXCEL_PATH = Path(__file__).parent.parent / "evaluation_report.xlsx"
YAML_PATH = Path(__file__).parent.parent / "test_cases.yaml"

# 输出文件保存到 ragas 文件夹
OUTPUT_DIR = Path(__file__).parent

# 输出文件（按领域自动选择）
DOMAIN = os.getenv("RAGAS_DOMAIN", "auto")  # auto / travel / civil


def get_domain_from_case_id(case_id: str) -> str:
    """从用例ID判断领域"""
    if case_id.startswith("TR"):
        return "travel"
    elif case_id.startswith("CC"):
        return "civil"
    elif case_id.startswith("RB") or case_id.startswith("AT"):
        return "robustness"
    return "travel"


def get_domain_label(domain: str) -> str:
    labels = {"travel": "旅游规划", "civil": "民法典", "robustness": "鲁棒性/安全性"}
    return labels.get(domain, "通用")

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_API_BASE = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com")
LLM_MODEL = os.getenv("RAGAS_LLM_MODEL", os.getenv("DEEPSEEK_MODEL", "deepseekv4flash"))

# RAGAS 将使用 DeepSeek 作为评判 LLM
# 不使用 ragas 库本身的 LLM 封装（避免兼容性问题），而是直接调用 API
# 实现 RAGAS 核心指标：answer_relevancy、answer_correctness


def load_excel_data() -> List[Dict]:
    """从 Excel 加载问题与回答"""
    if not EXCEL_PATH.exists():
        print(f"❌ Excel 文件不存在: {EXCEL_PATH}")
        return []

    wb = openpyxl.load_workbook(EXCEL_PATH)
    ws = wb.active
    headers = [cell.value for cell in ws[1]]

    data = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row[0] is None:
            continue
        item = {
            "case_id": str(row[0] or ""),
            "question": str(row[1] or ""),
            "answer": str(row[2] or ""),
            "existing_score": row[3],
            "existing_summary": str(row[4] or ""),
            "tool_context": str(row[7] or ""),  # 工具调用上下文（第8列）
        }
        data.append(item)

    wb.close()
    print(f"📊 从 Excel 加载 {len(data)} 条评测数据")
    return data


def load_yaml_ground_truth() -> Dict:
    """从 test_cases.yaml 加载 expected_keywords 作为 ground_truth"""
    if not YAML_PATH.exists():
        print(f"⚠️ YAML 文件不存在: {YAML_PATH}")
        return {}

    with open(YAML_PATH, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    cases = raw.get("test_cases", [])
    gt_map = {}
    for c in cases:
        case_id = c.get("id", "")
        keywords = c.get("expected_keywords", [])
        category = c.get("category", "")
        query = c.get("query", "")
        gt_map[case_id] = {
            "keywords": keywords,
            "category": category,
            "query": query,
        }

    print(f"📚 从 YAML 加载 {len(gt_map)} 条 ground truth (expected_keywords)")
    return gt_map


class DeepSeekLite:
    """DeepSeek API 轻量封装，用于 RAGAS 指标计算"""

    def __init__(self):
        self.api_key = DEEPSEEK_API_KEY or os.getenv("DEEPSEEK_API_KEY", "")
        self.api_base = DEEPSEEK_API_BASE
        self.model = LLM_MODEL
        self.client = httpx.AsyncClient(timeout=30.0)

        if not self.api_key:
            print("⚠️ DEEPSEEK_API_KEY 未设置，将使用模拟评分")

    async def generate(self, prompt: str, response_format: Optional[str] = None) -> str:
        """调用 DeepSeek API"""
        if not self.api_key:
            return '{"error": "API key not configured"}'

        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 2048,
        }
        if response_format == "json":
            payload["response_format"] = {"type": "json_object"}

        try:
            resp = await self.client.post(
                f"{self.api_base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except Exception as e:
            print(f"  ⚠️ DeepSeek API 调用失败: {e}")
            return json.dumps({"error": str(e)})

    async def close(self):
        await self.client.aclose()


# ============================================================
# RAGAS 指标实现
# ============================================================

async def compute_answer_relevancy(llm: DeepSeekLite, question: str, answer: str) -> Dict:
    """
    答案相关性 (Answer Relevancy)
    衡量答案与问题的相关程度。
    方法：让 LLM 评估答案是否针对问题、是否切题。
    返回 0-1 分数，越高越好。
    """
    prompt = f"""你是一个专业的 RAG 评估官。请评估以下"答案"与"问题"之间的相关性 (Answer Relevancy)。

注意：根据问题内容自动判断所属领域（旅游规划、法律咨询等），并按照对应领域的标准评估。

### ⚠️ 特别提示（法律领域评估）
对于编号以 CC 开头的案例（民法典评估集），助手可能正确指出问题**不属于民法典调整范围**，而是属于其他法律领域。如果助手正确识别了法律领域并给出引导，这是正确行为，不应扣分。不要因为助手没有给出民法典条文而扣分。

### 通用评分标准（0-1 分）
- 1.0: 回答完全切题，直接回答了用户问题，没有无关内容
- 0.8: 回答基本切题，大部分内容与问题相关，有少量延伸
- 0.6: 回答部分切题，有些内容与问题无关或答非所问
- 0.4: 回答与问题关联度低，大部分内容不相关
- 0.2: 回答与问题几乎无关
- 0.0: 完全无关

### 扣分项
- 答案是被安全过滤器拦截后的文案（「该问题涉及敏感信息」），直接 0 分
- 答案说「我不会」「无法处理」等拒绝回答，0.2 分
- 答案反问用户「你想去哪里」而没有先给出推荐，0.4 分

请输出 JSON 格式：
{{
  "score": 0.95,
  "reason": "简要说明评分的理由",
  "issues": ["问题 1", "问题 2"]
}}

问题：{question}

答案：{answer}
"""
    response = await llm.generate(prompt, response_format="json")
    try:
        result = json.loads(response)
        score = float(result.get("score", 0.5))
        return {
            "answer_relevancy": round(max(0.0, min(1.0, score)), 4),
            "relevancy_reason": result.get("reason", ""),
            "relevancy_issues": result.get("issues", []),
        }
    except (json.JSONDecodeError, ValueError, TypeError):
        return {"answer_relevancy": 0.5, "relevancy_reason": "评分解析失败", "relevancy_issues": []}


async def compute_answer_correctness(
    llm: DeepSeekLite, question: str, answer: str, ground_truth_keywords: List[str]
) -> Dict:
    """
    答案正确性 (Answer Correctness)
    衡量答案是否包含 ground_truth 中的关键信息。
    结合关键词覆盖 + LLM 语义判断。
    返回 0-1 分数，越高越好。
    """
    if not ground_truth_keywords:
        return {"answer_correctness": 0.0, "correctness_reason": "无 ground_truth 关键词", "keyword_coverage": 0.0}

    # 1. 关键词覆盖统计
    answer_lower = answer.lower()
    found_keywords = []
    missing_keywords = []
    for kw in ground_truth_keywords:
        if kw.lower() in answer_lower:
            found_keywords.append(kw)
        else:
            missing_keywords.append(kw)

    coverage = len(found_keywords) / max(len(ground_truth_keywords), 1)

    # 2. LLM 语义判断
    keywords_str = "、".join(ground_truth_keywords)
    prompt = f"""你是一个专业的 RAG 评估官。请评估以下"答案"在关键信息上的正确性 (Answer Correctness)。

### 🔑 核心原则：语义第一，关键词仅供参考
评估时请**充分理解问题和答案的语义**，不要机械地检查是否包含了某个关键词。
- 对于**引导性回答**（如「该问题不属于民法典范围，属于XX法调整」），应判断**法律领域的指引是否正确**，而非是否包含具体法条
- 对于**非民法典问题被正确拦截**，应视为正确行为，不应因「没有提供民法典条文」而扣分
- 对于**灰色地带问题**（既涉及行政处罚又涉及民事赔偿），助手如果并行回答了民事部分，即使也指出了行政部分，也是合理回答

### 评分标准（0-1 分）
- 1.0: 答案在语义上完全正确，提供了准确的法律指引或信息
- 0.8: 答案基本正确，有少量不准确但整体方向正确
- 0.6: 答案部分正确，有较明显遗漏或偏差但方向尚可
- 0.4: 答案偏离了问题核心，或提供了错误的法律指引
- 0.2: 答案几乎完全错误或拒绝回答
- 0.0: 完全无关或包含严重错误信息

请输出 JSON 格式：
{{
  "score": 0.85,
  "reason": "简要说明评分的理由（重点说明语义判断依据）",
  "semantic_hit": true  // 答案是否在语义上覆盖了关键信息
}}

问题：{question}

答案：{answer}
"""
    response = await llm.generate(prompt, response_format="json")
    try:
        result = json.loads(response)
        llm_score = float(result.get("score", 0.5))
        semantic_hit = result.get("semantic_hit", False)
    except (json.JSONDecodeError, ValueError, TypeError):
        llm_score = coverage
        semantic_hit = coverage > 0.3

    # 综合分数：关键词覆盖(40%) + LLM 语义判断(60%)
    final_score = coverage * 0.4 + llm_score * 0.6

    return {
        "answer_correctness": round(max(0.0, min(1.0, final_score)), 4),
        "correctness_reason": result.get("reason", "") if 'result' in dir() else "",
        "keyword_coverage": round(coverage, 4),
        "found_keywords": found_keywords,
        "missing_keywords": missing_keywords,
        "llm_score": round(llm_score, 4),
    }


async def compute_faithfulness(llm: DeepSeekLite, question: str, answer: str, tool_context: str = "") -> Dict:
    """
    忠实度 (Faithfulness) - 考虑工具调用上下文
    衡量答案是否包含幻觉。如果 Agent 调用了外部工具获取了真实数据，
    这些数据不应被视为幻觉。
    """
    context_section = ""
    if tool_context:
        context_section = f"""
### 工具调用上下文（Agent 通过以下工具获取了真实数据）
{tool_context[:2000]}

注意：以上是 Agent 通过工具调用获取的真实数据，不是幻觉。
答案中引用这些数据属于合理使用，不作为扣分项。
"""

    prompt = f"""你是一个专业的 RAG 评估官。请评估以下"答案"的忠实度 (Faithfulness) —— 即答案是否包含幻觉、矛盾或编造的信息。

### 上下文感知
- 如果答案中使用了工具返回的数据（如天气、酒店、餐厅等），这些数据是真实的，不算幻觉
- 如果是笼统的推荐（如「可以去天坛公园、颐和园」），这是常识性信息，不算幻觉
- 安全过滤器拦截文案（「该问题涉及敏感信息」）本身不是幻觉，但属于系统问题
{context_section}
评分标准（0-1 分）：
- 1.0: 答案完全忠实，没有幻觉、矛盾或编造内容
- 0.8: 答案基本忠实，有少量不精确表述但不构成严重问题
- 0.6: 答案存在一些可疑或无法验证的陈述
- 0.4: 答案包含明显的幻觉或编造的信息
- 0.2: 答案大部分内容不可信
- 0.0: 答案完全不可信

请输出 JSON 格式：
{{
  "score": 0.95,
  "reason": "简要说明",
  "hallucinations": ["幻觉 1"]  // 如有幻觉，列出具体内容
}}

问题：{question}

答案：{answer}
"""
    response = await llm.generate(prompt, response_format="json")
    try:
        result = json.loads(response)
        score = float(result.get("score", 0.5))
        return {
            "faithfulness": round(max(0.0, min(1.0, score)), 4),
            "faithfulness_reason": result.get("reason", ""),
            "hallucinations": result.get("hallucinations", []),
        }
    except (json.JSONDecodeError, ValueError, TypeError):
        return {"faithfulness": 0.5, "faithfulness_reason": "评分解析失败", "hallucinations": []}


# ============================================================
# 主流程
# ============================================================

async def run_evaluation():
    print("=" * 60)
    print("🔬 RAGAS 评估启动")
    print("=" * 60)

    # 1. 加载数据
    excel_data = load_excel_data()
    if not excel_data:
        print("❌ 没有评测数据，退出")
        return

    gt_map = load_yaml_ground_truth()

    # 2. 初始化 LLM
    llm = DeepSeekLite()

    # 3. 逐条评估
    results = []
    total = len(excel_data)

    for idx, item in enumerate(excel_data):
        case_id = item["case_id"]
        question = item["question"]
        answer = item["answer"]

        print(f"\n[{idx + 1}/{total}] {case_id}: {question[:40]}...")

        if not answer or len(answer.strip()) < 10:
            print(f"  ⏭️ 跳过（回答为空或过短）")
            results.append({
                **item,
                "answer_relevancy": 0.0,
                "answer_correctness": 0.0,
                "faithfulness": 0.0,
                "relevancy_reason": "回答为空",
                "correctness_reason": "回答为空",
                "faithfulness_reason": "回答为空",
                "keyword_coverage": 0.0,
                "found_keywords": [],
                "missing_keywords": [],
                "hallucinations": [],
            })
            continue

        # 获取 ground_truth 关键词
        gt_info = gt_map.get(case_id, {})
        keywords = gt_info.get("keywords", [])

        # 并行计算所有指标
        relevancy_task = compute_answer_relevancy(llm, question, answer)
        correctness_task = compute_answer_correctness(llm, question, answer, keywords)
        faithfulness_task = compute_faithfulness(llm, question, answer, item.get("tool_context", ""))

        relevancy, correctness, faithfulness = await asyncio.gather(
            relevancy_task, correctness_task, faithfulness_task
        )

        result = {
            **item,
            **relevancy,
            **correctness,
            **faithfulness,
        }
        results.append(result)

        print(f"  📊 AR={result['answer_relevancy']:.3f} | "
              f"AC={result['answer_correctness']:.3f} | "
              f"FA={result['faithfulness']:.3f}")

    # 4. 关闭 LLM 客户端
    await llm.close()

    # 5. 输出结果（按领域保存）
    # 判断全局领域或从用例自动推断
    global_domain = DOMAIN if DOMAIN != "auto" else ""
    for item in results:
        d = get_domain_from_case_id(item.get("case_id", ""))
        item["_domain"] = d

    # 按领域分组保存
    from collections import defaultdict
    domain_groups = defaultdict(list)
    for item in results:
        domain_groups[item["_domain"]].append(item)

    for domain, group in domain_groups.items():
        label = get_domain_label(domain)
        out_path = OUTPUT_DIR / f"ragas_report_{domain}.xlsx"
        print(f"\n💾 保存 {label} 报告 ({len(group)} 条): {out_path.name}")
        await save_results_to_file(group, out_path)

    # 6. 打印汇总
    print_summary(results)


def print_summary(results: List[Dict]):
    """打印汇总统计"""
    if not results:
        return

    print("\n" + "=" * 60)
    print("📈 RAGAS 评估汇总")
    print("=" * 60)

    valid = [r for r in results if r.get("answer_relevancy", 0) > 0 or r.get("answer_correctness", 0) > 0]

    if not valid:
        print("⚠️ 无有效评分数据")
        return

    ar_scores = [r["answer_relevancy"] for r in valid]
    ac_scores = [r["answer_correctness"] for r in valid]
    fa_scores = [r["faithfulness"] for r in valid]

    avg_ar = sum(ar_scores) / len(ar_scores)
    avg_ac = sum(ac_scores) / len(ac_scores)
    avg_fa = sum(fa_scores) / len(fa_scores)

    print(f"  评估总数: {len(results)}")
    print(f"  有效评分: {len(valid)}")
    print(f"  Answer Relevancy (答案相关性)  : {avg_ar:.4f}")
    print(f"  Answer Correctness (答案正确性): {avg_ac:.4f}")
    print(f"  Faithfulness (忠实度)          : {avg_fa:.4f}")
    print(f"  RAGAS 综合得分                 : {(avg_ar + avg_ac + avg_fa) / 3:.4f}")
    print()

    # 按现有评分分组（防御性转换）
    def _safe_float(v):
        try: return float(v) if v not in (None, '-', '') else None
        except: return None

    if any(r.get("existing_score") for r in valid):
        high = [r for r in valid if _safe_float(r.get("existing_score")) is not None and _safe_float(r.get("existing_score")) >= 9]
        mid = [r for r in valid if _safe_float(r.get("existing_score")) is not None and 7 <= _safe_float(r.get("existing_score")) < 9]
        low = [r for r in valid if _safe_float(r.get("existing_score")) is not None and _safe_float(r.get("existing_score")) < 7]
        if high:
            print(f"  现有评分 ≥9 的 {len(high)} 条:")
            print(f"    AR={sum(r['answer_relevancy'] for r in high)/len(high):.4f} | "
                  f"AC={sum(r['answer_correctness'] for r in high)/len(high):.4f} | "
                  f"FA={sum(r['faithfulness'] for r in high)/len(high):.4f}")
        if mid:
            print(f"  现有评分 7-9 的 {len(mid)} 条:")
            print(f"    AR={sum(r['answer_relevancy'] for r in mid)/len(mid):.4f} | "
                  f"AC={sum(r['answer_correctness'] for r in mid)/len(mid):.4f} | "
                  f"FA={sum(r['faithfulness'] for r in mid)/len(mid):.4f}")
        if low:
            print(f"  现有评分 <7 的 {len(low)} 条:")
            print(f"    AR={sum(r['answer_relevancy'] for r in low)/len(low):.4f} | "
                  f"AC={sum(r['answer_correctness'] for r in low)/len(low):.4f} | "
                  f"FA={sum(r['faithfulness'] for r in low)/len(low):.4f}")


async def save_results_to_file(results: List[Dict], output_path: Path):
    """保存评估结果到 Excel"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "RAGAS 评估结果"

    # 表头
    headers = [
        "用例ID", "用户问题", "Agent回答(完整)",
        "Answer Relevancy", "相关性说明",
        "Answer Correctness", "正确性说明", "关键词覆盖率",
        "命中的关键词", "缺失的关键词",
        "Faithfulness", "忠实度说明", "幻觉列表",
        "现有评分(10分制)", "RAGAS综合分",
    ]

    # 样式
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="2D8A4E", end_color="2D8A4E", fill_type="solid")
    header_alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_alignment

    # 数据行
    green_fill = PatternFill(start_color="E8F5E9", end_color="E8F5E9", fill_type="solid")
    red_fill = PatternFill(start_color="FFEBEE", end_color="FFEBEE", fill_type="solid")

    for row_idx, r in enumerate(results, 2):
        answer_preview = r.get("answer", "")

        ragas_composite = (r.get("answer_relevancy", 0) + r.get("answer_correctness", 0) + r.get("faithfulness", 0)) / 3

        row_data = [
            r.get("case_id", ""),
            r.get("question", ""),
            answer_preview,
            r.get("answer_relevancy", 0),
            r.get("relevancy_reason", ""),
            r.get("answer_correctness", 0),
            r.get("correctness_reason", ""),
            r.get("keyword_coverage", 0),
            ", ".join(r.get("found_keywords", [])),
            ", ".join(r.get("missing_keywords", [])),
            r.get("faithfulness", 0),
            r.get("faithfulness_reason", ""),
            ", ".join(r.get("hallucinations", [])),
            r.get("existing_score", ""),
            round(ragas_composite, 4),
        ]

        for col, value in enumerate(row_data, 1):
            cell = ws.cell(row=row_idx, column=col, value=value)
            cell.alignment = Alignment(wrap_text=True, vertical="top")

            # 条件着色：分数列
            if col in (4, 6, 11, 15):  # 分数列
                try:
                    score_val = float(value) if value else 0
                    cell.fill = green_fill if score_val >= 0.7 else red_fill
                except (ValueError, TypeError):
                    pass

    # 列宽
    col_widths = [12, 30, 40, 14, 30, 14, 30, 14, 20, 20, 14, 30, 20, 14, 14]
    for i, width in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = width

    # 冻结首行
    ws.freeze_panes = "A2"

    # 自动筛选
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(results) + 1}"

    wb.save(str(output_path))
    print(f"\n💾 评估报告已保存: {output_path}")


# ============================================================
# 入口
# ============================================================

def main():
    """同步入口"""
    asyncio.run(run_evaluation())


if __name__ == "__main__":
    main()
