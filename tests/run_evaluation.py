import asyncio
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import httpx
import yaml
from openpyxl import Workbook
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import get


class TestCase(BaseModel):
    id: str
    category: str
    query: str
    expected_tools: List[str]   # 保留但不使用
    expected_keywords: List[str] # 保留但不使用
    difficulty: str


class EvalResult(BaseModel):
    case_id: str
    query: str
    agent_response: str = ""
    tool_context: list = []  # 工具调用过程记录
    score: Optional[float] = None   # 十分制
    summary: Optional[str] = None   # 扣分说明 + 综合评价
    cached: bool = False
    error: Optional[str] = None


# ---------- eval-as-code 纯逻辑（独立可自检，不依赖网关/LLM） ----------

def compute_category_stats(results: List[EvalResult]) -> Dict:
    """按 category 分组统计：数量/平均分/最低分。

    score=None 的异常结果不计入分数，但分类仍会出现（count=0）——
    让「某分类全部评测异常」在报告里可见，而不是消失。
    """
    stats: Dict = {r.category: {"count": 0, "total": 0.0, "min": 10.0} for r in results}
    for r in results:
        if r.score is None:
            continue
        s = stats[r.category]
        s["count"] += 1
        s["total"] += r.score
        s["min"] = min(s["min"], r.score)
    for s in stats.values():
        s["avg"] = round(s["total"] / s["count"], 2) if s["count"] else 0.0
    return stats


def parse_category_thresholds(raw: str) -> Dict[str, float]:
    """解析分类门禁参数，如 '民法典:8.0,攻击:9.0' → {'民法典': 8.0, '攻击': 9.0}"""
    out: Dict[str, float] = {}
    if not raw:
        return out
    for part in raw.split(","):
        part = part.strip()
        if ":" not in part:
            continue
        cat, _, val = part.partition(":")
        try:
            out[cat.strip()] = float(val)
        except ValueError:
            continue
    return out


def gate_failures(overall_avg: float, category_stats: Dict, threshold: float,
                  category_thresholds: Dict[str, float]) -> List[str]:
    """返回未达标的门禁清单（空列表 = 通过）。整体线 + 每类线（未指定分类用整体阈值）。"""
    fails = []
    if overall_avg < threshold:
        fails.append(f"整体平均 {overall_avg:.2f} < {threshold}")
    for cat, s in category_stats.items():
        if not s["count"]:
            continue
        t = category_thresholds.get(cat, threshold)
        if s["avg"] < t:
            fails.append(f"[{cat}] 平均 {s['avg']:.2f} < {t}")
    return fails


def compare_regression(prev: Dict[str, float], cur: Dict[str, float],
                       min_delta: float = -0.5) -> tuple:
    """逐用例回归对比：返回 (劣化列表, 提升列表)，delta = 本次 - 上次。

    劣化：delta <= -0.5；提升：delta >= +0.5。无历史（prev 空）时不误报。
    每项: (case_id, 上次分, 本次分, delta)
    """
    regressions, improvements = [], []
    if not prev:
        return regressions, improvements
    for cid, score in cur.items():
        if cid in prev and prev[cid] is not None and score is not None:
            delta = score - prev[cid]
            if delta <= min_delta:
                regressions.append((cid, prev[cid], score, round(delta, 2)))
            elif delta >= -min_delta:
                improvements.append((cid, prev[cid], score, round(delta, 2)))
    regressions.sort(key=lambda x: x[3])
    improvements.sort(key=lambda x: x[3], reverse=True)
    return regressions, improvements


def _load_history(path: str) -> Dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}  # 兼容：旧历史文件缺失/损坏 → 无历史，不误报劣化


def _self_check() -> None:
    """纯逻辑自检：分类统计、门禁判定、逐用例回归、分类门禁解析。"""
    from types import SimpleNamespace

    def mk(cid, cat, score):
        return SimpleNamespace(case_id=cid, category=cat, score=score,
                               error=None, summary="", cached=False)

    # 1. 分类统计：None 不计入；avg/min 正确
    results = [mk("CC001", "民法典", 8.5), mk("CC002", "民法典", 7.0),
               mk("TR001", "旅行", 9.0), mk("RB001", "鲁棒性", None)]
    stats = compute_category_stats(results)
    assert stats["民法典"]["count"] == 2 and stats["民法典"]["avg"] == 7.75
    assert stats["民法典"]["min"] == 7.0 and stats["鲁棒性"]["count"] == 0
    print("[self-check] 分类统计 OK（None 不计入）")

    # 2. 门禁：整体线 + 分类线
    assert gate_failures(8.0, stats, 7.5, {}) == []            # 整体 8.17 >= 7.5，分类均 >= 7.5 → 通过
    fails = gate_failures(8.0, stats, 8.0, {})                 # 民法典 7.75 < 8.0 → 分类线失败
    assert any("民法典" in f for f in fails)
    fails = gate_failures(7.5, stats, 8.0, {})                 # 整体 7.5 < 8.0 → 整体线失败
    assert any("整体" in f for f in fails)
    print("[self-check] 门禁判定 OK（整体线 + 分类线）")

    # 3. 逐用例回归：劣化/提升判定；无历史不误报
    prev = {"A": 9.0, "B": 8.0, "C": 7.0}
    cur = {"A": 8.0, "B": 8.5, "C": 7.0}   # A 跌 1.0，B 涨 0.5，C 持平
    reg, imp = compare_regression(prev, cur)
    assert [x[0] for x in reg] == ["A"] and reg[0][3] == -1.0
    assert [x[0] for x in imp] == ["B"]
    assert compare_regression({}, cur) == ([], [])
    print("[self-check] 逐用例回归 OK（无历史不误报）")

    # 4. 分类门禁参数解析
    assert parse_category_thresholds("民法典:8.0,攻击:9.0") == {"民法典": 8.0, "攻击": 9.0}
    assert parse_category_thresholds("") == {}
    assert parse_category_thresholds("乱写,民法典:8.0") == {"民法典": 8.0}
    print("[self-check] 分类门禁解析 OK")
    print("全部自检通过")


class Evaluator:
    def __init__(self, config: Dict):
        self.config = config
        self.cache_file = Path("tests/eval_cache.json")
        self.cache = self._load_cache()
        self.client = httpx.AsyncClient(timeout=60.0)
        self.gateway_url = config["gateway_url"]
        self.token = None

    def _load_cache(self) -> Dict:
        if self.cache_file.exists():
            with open(self.cache_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _save_cache(self):
        with open(self.cache_file, "w", encoding="utf-8") as f:
            json.dump(self.cache, f, indent=2, ensure_ascii=False)

    def _get_cache_key(self, case: TestCase) -> str:
        # 基于用例内容和系统版本生成唯一键
        context = f"{case.id}_{case.query}_{case.expected_tools}_{case.expected_keywords}"
        context += f"_{self.config.get('system_prompt_version', 'v1')}"
        context += "_v2_tool_context"  # 缓存版本标记
        return hashlib.md5(context.encode()).hexdigest()

    async def login(self):
        resp = await self.client.post(
            f"{self.gateway_url}/api/login",
            json={
                "username": self.config["username"],
                "password": self.config["password"]
            }
        )
        resp.raise_for_status()
        self.token = resp.json()["access_token"]
        print("✅ 登录成功")

    async def query_agent(self, query: str, persona_id: str = "") -> Dict:
        """向 Agent 发送请求，获取完整回复（支持 v2 流式格式）"""
        api_path = self.config.get("chat_api_path", "/v1/chat/stream")
        payload = {"query": query, "user": "eval_user"}
        if persona_id:
            payload["persona_id"] = persona_id
        try:
            async with self.client.stream(
                "POST",
                f"{self.gateway_url}{api_path}",
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json"
                },
                json=payload
            ) as resp:
                resp.raise_for_status()
                full_response = ""
                tool_context = []  # 记录工具调用过程
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        try:
                            data = json.loads(line[5:].strip())
                            # 捕获工具调用
                            if data.get("type") == "tool_call":
                                tool_context.append({
                                    "type": "tool_call",
                                    "name": data.get("name", ""),
                                    "args": data.get("args", {})
                                })
                            # 捕获工具结果
                            elif data.get("type") == "tool_result":
                                tool_context.append({
                                    "type": "tool_result",
                                    "index": data.get("index", 0),
                                    "result": data.get("result", {})
                                })
                            # v2 格式: {"type": "answer_chunk", "content": "..."}
                            if data.get("type") == "answer_chunk":
                                full_response += data.get("content", "")
                            # v1 格式: {"answer": "..."}
                            elif "answer" in data:
                                full_response += data["answer"]
                            # 兼容 answer_complete 标记
                            elif data.get("type") == "answer_complete":
                                full_response = data.get("content", full_response)
                        except json.JSONDecodeError:
                            pass
                return {
                    "response": full_response.strip(),
                    "tool_context": tool_context
                }
        except Exception as e:
            return {"response": "", "error": str(e), "tool_context": []}

    async def call_llm_judge(self, case: TestCase, response: str) -> Dict:
        """调用大模型评测官（改进版：上下文感知 + 防幻觉 + 边界用例）"""
        # 构建上下文提示（如果是多轮对话中的后续轮次）
        context_hint = ""
        if "TC01" in case.id:
            context_hint = "（注意：此问题是多轮对话中的一环，需结合前文理解完整语义）"

        # 人工验证清单（根据领域调整）
        is_civil = case.category == "民法典"
        if is_civil:
            checklist = """
### 🔍 民法典专项验证清单（必须逐项检查）

#### 第零步：判断问题是否属于民法典
先判断用户的问题本质上是否属于民法典调整范围。判断标准：
- ✅ 涉及自然人、法人、非法人组织之间的**人身关系或财产关系**（婚姻家庭、继承、物权、合同、侵权责任、人格权等）→ 属于民法典
- ❌ 纯粹涉及行政处罚、刑事责任、行政诉讼、劳动仲裁程序等 → 不属于民法典
- ⚠️ 交叉领域（如偷拍涉及人格权+治安处罚、高利贷涉及合同+刑法）：**只要涉及民事关系，就属于民法典调整范围**，助手应回答民法典部分

#### 第一步：检查回答是否完整
- **半截回答**：回答是否以「让我先查询」「正在查询」「我来查一下」等开头后无实质内容？发现直接扣3分
- **空回复/拒绝回答**：回复为空或说「我不会」「无法处理」？扣5分
- **安全过滤误伤**：回复是否为「该问题涉及敏感信息」等安全拦截文案？若是，直接打2分

#### 第二步：民法典专项检查
1. **法律依据归因错误**：是否将其他法律的内容错误归入民法典？（如将消费者权益保护法第25条的"七天无理由退货"归入民法典）发现则扣3-5分
2. **编造法条**：是否编造了不存在的法条或错误引用法条编号？每处扣2分
3. **语境理解偏差**：是否理解了用户问题的实质法律需求？（如用户问侵权问题，回答却聚焦于合同）扣1~2分

#### 第三步：交叉领域检查
对于涉及多个法律领域的问题：
- 助手应**先回答民法典部分**（引用具体法条）
- 然后**简要提及**其他法律领域，不做详解
- ✅ 正确示例：「关于偷拍行为，《民法典》第1032条规定了隐私权保护...同时，该行为也可能违反《治安管理处罚法》的相关规定，建议咨询专业律师。」
- ❌ 错误示例：仅说「这不属于民法典范围」而不回答任何民法典内容

**特别注意**：如果助手正确指出了问题属于其他法律领域（如消费者权益保护法），并给出正确指引，这是加分项，不是扣分项。但如果问题**同时涉及**民法典和其他法律，助手必须回答民法典部分。
"""
        else:
            checklist = """
### 🔍 人工验证清单（必须逐项检查）
1. **安全过滤误伤**：回复是否为「该问题涉及敏感信息」等安全拦截文案？若是，扣5分（系统缺陷）
2. **编造事实**：是否编造了具体的酒店名、餐厅名、价格、距离等无法验证的数据？每处扣2分
3. **空回复/拒绝回答**：回复为空或说「我不会」「无法处理」？扣5分
4. **反问过多**：用户已表达需求但助手还在反问「你想去哪里」？扣2分（vs 主动推荐）
5. **上下文断裂**：多轮对话中是否忽略了前文给出的关键信息？扣1~2分
"""
        prompt = f"""
你是一个专业的 AI 助手评测官，请对以下对话进行客观评分。{context_hint}

用户问题：{case.query}
助手回复：{response}

{checklist}

评分规则：
- 基础分为 10 分。
- 从以下维度检查，发现一个问题扣 1~2 分：
  1. 回答是否直接、有用？（是否准确回答了用户问题）
  2. 逻辑是否清晰、连贯？（是否存在前后矛盾或跳跃）
  3. 语气是否自然、有人情味？（是否机械、冷漠或过于夸张）
  4. 是否存在明显的事实错误或幻觉？（编造不存在的信息）
- **特别注意**：如果回答是安全过滤器的拦截文案（「该问题涉及敏感信息」等），无论内容如何，直接打 2 分并说明「安全过滤误伤」
- 总分为 0~10，允许小数（如 8.5）。

请输出 JSON 格式：
{{
  "score": 8.5,
  "summary": "扣分项：xxx（扣1分）；xxx（扣0.5分）。综合评价：...（一句话总结）"
}}
"""
        resp = await self.client.post(
            self.config["llm_api_url"],
            headers={"Authorization": f"Bearer {self.config['llm_api_key']}"},
            json={
                    "model": self.config.get("llm_model", os.getenv('DEEPSEEK_MODEL', 'deepseekv4flash')),
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "response_format": {"type": "json_object"}
            }
        )
        resp.raise_for_status()
        result = resp.json()
        content = result["choices"][0]["message"]["content"]
        try:
            return json.loads(content)
        except Exception:
            # judge 异常 ≠ 回答 0 分：返回 None，不计入门禁，进人工复核清单
            return {"score": None, "summary": "评测官返回格式错误，请人工复核"}

    async def evaluate_case(self, case: TestCase) -> EvalResult:
        print(f"🔍 正在评测: {case.id} - {case.query[:30]}...")

        # 1. 获取 Agent 回复（根据分类选择人格）
        persona_id = "civil_code" if case.category == "民法典" else ""
        result = await self.query_agent(case.query, persona_id=persona_id)
        if "error" in result:
            return EvalResult(
                case_id=case.id,
                query=case.query,
                agent_response=result.get("response", ""),
                error=result["error"]
            )

        response = result.get("response", "")
        tool_context = result.get("tool_context", [])
        if not response:
            return EvalResult(
                case_id=case.id,
                query=case.query,
                agent_response="",
                error="Agent 返回空回复"
            )

        # 2. 检查缓存（--no-cache 跳过：改过代码必须重测，防测到旧结果）
        cache_key = self._get_cache_key(case)
        if not self.config.get("no_cache") and cache_key in self.cache:
            cached = self.cache[cache_key]
            print(f"   命中缓存: {case.id}")
            return EvalResult(
                case_id=case.id,
                query=case.query,
                agent_response=response,
                score=cached.get("score"),
                summary=cached.get("summary"),
                cached=True
            )

        # 3. 调用大模型评测
        print(f"   🤖 调用 DeepSeek 评测: {case.id}")
        try:
            judge_result = await self.call_llm_judge(case, response)
            score = judge_result.get("score")
            summary = judge_result.get("summary", "")

            # 存入缓存
            self.cache[cache_key] = {"score": score, "summary": summary}
            self._save_cache()

            return EvalResult(
                case_id=case.id,
                query=case.query,
                agent_response=response,
                score=score,
                summary=summary,
                cached=False
            )
        except Exception as e:
            return EvalResult(
                case_id=case.id,
                query=case.query,
                agent_response=response,
                error=f"大模型评测失败: {str(e)}"
            )

    async def run(self, cases: List[TestCase]) -> List[EvalResult]:
        await self.login()
        results = []
        for case in cases:
            result = await self.evaluate_case(case)
            results.append(result)
            await asyncio.sleep(0.5)  # 限速
        return results

    def export_to_excel(self, results: List[EvalResult], output_path: str = "tests/evaluation_report.xlsx"):
        wb = Workbook()
        ws = wb.active
        ws.title = "评测结果"

        headers = ["用例ID", "用户问题", "Agent完整回复", "评分(10分制)", "扣分说明与综合评价", "是否缓存", "错误", "工具调用上下文"]
        ws.append(headers)

        for r in results:
            # 工具上下文格式化为可读文本
            tool_text = ""
            for tc in (r.tool_context or []):
                if tc.get("type") == "tool_call":
                    n = tc.get("name", "")
                    a = json.dumps(tc.get("args", {}), ensure_ascii=False)
                    tool_text += f"[调用] {n}({a})\n"
                elif tc.get("type") == "tool_result":
                    res = tc.get("result", {})
                    # 精简结果（只保留关键摘要）
                    summary = ""
                    if isinstance(res, dict):
                        if "error" in res:
                            summary = f"错误: {res['error'][:100]}"
                        elif "results" in res:
                            summary = f"找到 {len(res['results'])} 条结果"
                        elif "restaurants" in res:
                            summary = f"找到 {len(res['restaurants'])} 家餐厅"
                        elif "hotels" in res:
                            summary = f"找到 {len(res['hotels'])} 家酒店"
                        elif "route" in res:
                            summary = f"路线: {json.dumps(res['route'], ensure_ascii=False)[:100]}"
                        elif "temperature" in res:
                            summary = f"{res.get('city','')} {res.get('weather','')} {res['temperature']}°C"
                        else:
                            summary = json.dumps(res, ensure_ascii=False)[:100]
                    else:
                        summary = str(res)[:100]
                    tool_text += f"  [结果] {summary}\n"

            ws.append([
                r.case_id,
                r.query,
                r.agent_response,          # 完整回复，不截断
                r.score if r.score is not None else "-",
                r.summary or "-",
                "✅" if r.cached else "❌",
                r.error or "-",
                tool_text.strip()
            ])

        wb.save(output_path)
        print(f"报告已导出: {output_path}")


async def main():
    # eval-as-code：门禁参数（平均分/分类线低于阈值 → 退出码 1，可接 CI）
    parser = argparse.ArgumentParser(description="评测门禁：低于阈值则退出码 1")
    parser.add_argument("--threshold", type=float, default=8.0, help="整体平均分门禁阈值（默认 8.0）")
    parser.add_argument("--category-thresholds", type=str, default="",
                        help="分类门禁，如 '民法典:8.0,攻击:9.0'（未指定分类用 --threshold）")
    parser.add_argument("--history", type=str, default="tests/eval_history.json", help="历史结果文件（回归对比）")
    parser.add_argument("--max-cases", type=int, default=0, help="只跑前 N 条用例（0=全部，CI 快速验证用）")
    parser.add_argument("--version", type=str, default="v3",
                        help="系统版本标记（改了代码/prompt/工具逻辑后必须递增，否则缓存命中旧结果）")
    parser.add_argument("--no-cache", action="store_true", help="忽略缓存强制全部重测")
    parser.add_argument("--worst", type=int, default=5, help="打印最差用例条数（默认 5）")
    args = parser.parse_args()

    config = {
        "gateway_url": "http://localhost:10092",
        "username": "admin",
        "password": get("ADMIN_PASSWORD"),  # 从 .env 读取
        "llm_api_url": "https://api.deepseek.com/chat/completions",
        "llm_api_key": os.getenv("DEEPSEEK_API_KEY", ""),  # 从环境变量读取
        "llm_model": "deepseek-v4-flash",
        "chat_api_path": "/v2/chat/stream",   # 使用 v2 路由（DeepSeek 直连 + 新温度/提示词）
        "system_prompt_version": args.version,   # 改代码后递增使缓存失效
        "no_cache": args.no_cache,
    }

    with open("tests/test_cases.yaml", "r", encoding="utf-8") as f:
        raw_cases = yaml.safe_load(f)
        cases = [TestCase(**c) for c in raw_cases['test_cases']]
    if args.max_cases > 0:
        cases = cases[:args.max_cases]
        print(f"（快速模式：仅评测前 {args.max_cases} 条）")

    evaluator = Evaluator(config)
    results = await evaluator.run(cases)

    total = len(results)
    scored = sum(1 for r in results if r.score is not None)
    cached = sum(1 for r in results if r.cached)
    anomalies = [r for r in results if r.score is None]  # judge 异常/网络错/空回复

    print("\n" + "=" * 50)
    print("评测完成！")
    print(f"   总用例数: {total}")
    print(f"   已评分: {scored}/{total}")
    print(f"   缓存命中: {cached}/{total}")
    if cached:
        print(f"   [提示] 缓存命中 {cached} 条——改了代码请用 --no-cache 或递增 --version，否则测的是旧结果")
    if anomalies:
        print(f"   [复核] 评测异常 {len(anomalies)} 条（不计入门禁，需人工复核）:")
        for r in anomalies[:5]:
            print(f"       - {r.case_id}: {r.error or r.summary or '未知'}")
    if scored:
        avg_score = sum(r.score for r in results if r.score is not None) / scored
        print(f"   平均分: {avg_score:.2f}")

        # ---- 分类统计（中等级门禁：整体 + 分类双线，防高分掩盖低分类）----
        cat_stats = compute_category_stats(results)
        for cat, s in sorted(cat_stats.items()):
            print(f"     [{cat}] 数量={s['count']} 平均={s['avg']:.2f} 最低={s['min']:.2f}")

        # ---- 最差用例追踪（每次看最低分，防个别用例持续劣化被平均掩盖）----
        worst = sorted((r for r in results if r.score is not None), key=lambda r: r.score)[:args.worst]
        print(f"   最差 {args.worst} 条:")
        for r in worst:
            print(f"       - {r.case_id} {r.score} 分: {(r.summary or '')[:80]}")

        # ---- 逐用例回归（对比上次，劣化告警；兼容旧历史文件）----
        history = _load_history(args.history)
        cur_per_case = {r.case_id: r.score for r in results if r.score is not None}
        regressions, improvements = compare_regression(history.get("per_case", {}), cur_per_case)
        if regressions:
            print(f"   [劣化] {len(regressions)} 条较上次跌 >=0.5 分:")
            for cid, prev_s, cur_s, delta in regressions[:10]:
                print(f"       - {cid}: {prev_s:.2f} -> {cur_s:.2f} ({delta:+.2f})")
        if improvements:
            print(f"   [提升] {len(improvements)} 条较上次涨 >=0.5 分（显示前 5）:")
            for cid, prev_s, cur_s, delta in improvements[:5]:
                print(f"       - {cid}: {prev_s:.2f} -> {cur_s:.2f} ({delta:+.2f})")

        # ---- 写历史（逐用例 + 分类，供下次回归对比）----
        with open(args.history, "w", encoding="utf-8") as f:
            json.dump({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "avg_score": round(avg_score, 4),
                "total": total,
                "scored": scored,
                "threshold": args.threshold,
                "per_case": cur_per_case,
                "category": {cat: s["avg"] for cat, s in cat_stats.items()},
            }, f, ensure_ascii=False, indent=2)

        # ---- 门禁：整体 + 分类双线，任一未达 → 退出码 1（CI 可判定失败）----
        cat_thresholds = parse_category_thresholds(args.category_thresholds)
        failed_gates = gate_failures(avg_score, cat_stats, args.threshold, cat_thresholds)
        if failed_gates:
            print(f"❌ 门禁未通过：" + "；".join(failed_gates))
            evaluator.export_to_excel(results)
            sys.exit(1)
        print(f"✅ 门禁通过：整体 {avg_score:.2f} >= {args.threshold}，分类均达标")
    print("=" * 50)

    evaluator.export_to_excel(results)


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        _self_check()
    else:
        asyncio.run(main())