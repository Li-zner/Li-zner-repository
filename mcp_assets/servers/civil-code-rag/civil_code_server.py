"""
民法典知识检索 MCP Server（自包含独立实现）— mcp_assets 资产库 #001

设计（与用户确认的边界）：
- 范围：只封装 1 个检索 Tool，内部完整保留生产版四步逻辑
- 依赖：自包含 —— 不 import 项目内部模块，只复用根目录 _env.py 读配置 + tests/ 数据文件
- 数据库：连 docker 内 PostgreSQL（knowledge_chunks 表，source='civil_code' 861 行）

四步检索流水线（对应 app/agents/tools.py 的 search_knowledge）：
  ① 法律依据纠正映射守卫（tests/民法典补充协议.txt）
  ② 口语化表述 → 专业术语映射（tests/民法典映射表.txt）
  ③ pg_trgm 宽召回 + 关键词补充（knowledge_chunks 表）
  ④ 相似度低于阈值时 DeepSeek Rerank 精排（省 token 策略）

运行（stdio 传输，供任意 MCP Host / Client 拉起）：
    python civil_code_server.py

调用示例（Client 端）：
    await session.call_tool("search_civil_code", {"query": "离婚财产怎么分", "top_k": 3})
"""
import asyncio
import os
import re
import sys
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

# ============================================================
# 0. 配置与路径（复用根目录 _env.py，从 WSL 安全目录读权威配置）
# ============================================================
_ROOT = Path(__file__).resolve().parent.parent.parent.parent  # → 项目根
sys.path.insert(0, str(_ROOT))
from _env import get, db_url  # noqa: E402

_TESTS_DIR = _ROOT / "tests"
_MAPPING_FILE = _TESTS_DIR / "民法典补充协议.txt"      # 法律误区映射表
_COLLOQUIAL_FILE = _TESTS_DIR / "民法典映射表.txt"     # 口语→术语映射表

RERANK_SIM_THRESHOLD = 0.6   # 与原 config.py 默认一致：高于此相似度跳过 Rerank（省 token）
DEEPSEEK_API_BASE = "https://api.deepseek.com"
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

mcp = FastMCP("civil-code-rag")

# ============================================================
# ① 法律依据纠正映射守卫（移植自 app/agents/law_mapping.py）
# ============================================================
_LAW_MAPPING_CACHE: list[dict] | None = None


def _extract_keywords(text: str) -> list:
    text = re.sub(r"[/、，。；：\"\"''（）()/·]", " ", text)
    stop = {"什么", "怎么", "如何", "哪些", "是否", "可以", "需要", "相关", "请问"}
    return [w for w in text.split() if len(w.strip()) >= 2 and w.strip() not in stop]


def _load_law_mapping() -> list:
    """加载 tests/民法典补充协议.txt 的法律误区映射表"""
    global _LAW_MAPPING_CACHE
    if _LAW_MAPPING_CACHE is not None:
        return _LAW_MAPPING_CACHE
    entries = []
    if _MAPPING_FILE.exists():
        import csv
        with open(_MAPPING_FILE, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f, delimiter="\t"):
                scenario = (row.get("用户常见问法/场景") or "").strip()
                if not scenario:
                    continue
                entries.append({
                    "id": len(entries) + 1,
                    "scenario": scenario,
                    "law": (row.get("实际法律依据") or "").strip(),
                    "citation": (row.get("正确引用索引") or "").strip(),
                    "wrong_area": (row.get("常误认为《民法典》条文") or "").strip(),
                    "keywords": _extract_keywords(scenario),
                })
    _LAW_MAPPING_CACHE = entries
    return entries


def _match_score(query: str, keywords: list) -> float:
    """精确子串 + 子词匹配的加权评分（移植原实现）"""
    if not keywords:
        return 0.0
    exact = sum(1 for kw in keywords if kw in query)
    sub_matched, sub_total = 0, 0
    for kw in keywords:
        if len(kw) <= 2:
            continue
        for win_size in range(4, 1, -1):
            for i in range(len(kw) - win_size + 1):
                sub = kw[i:i + win_size]
                if len(sub) < 2:
                    continue
                sub_total += 1
                if sub in query:
                    sub_matched += 1
    if exact >= 2:
        return 1.0 + exact / len(keywords) * 0.2
    if exact == 1 and sub_total > 0:
        return 0.6 + sub_matched / max(sub_total, 1) * 0.3
    if exact == 1:
        return 0.6
    if sub_total > 0:
        return sub_matched / max(sub_total, 1) * 0.4
    return 0.0


def check_law_mapping(query: str, threshold: float = 0.3) -> dict | None:
    """用户问题命中法律误区 → 返回纠正引导；明确民法典问题跳过"""
    if "民法典" in query or re.search(r"第[0-9零一二三四五六七八九十百千]+条", query):
        return None
    best, best_score = None, 0.0
    for entry in _load_law_mapping():
        score = _match_score(query, entry["keywords"])
        if score > best_score:
            best_score, best = score, entry
    if best and best_score >= threshold:
        return {
            "matched": True,
            "id": best["id"],
            "scenario": best["scenario"],
            "law": best["law"],
            "citation": best["citation"],
            "wrong_area": best["wrong_area"],
            "message": (f"您好，我是民法典助手！您的问题属于{best['law']}"
                        f"（{best['citation']}）调整的范畴，请咨询相关领域的法律专业人士。"),
        }
    return None

# ============================================================
# ② 口语化表述 → 专业术语映射（移植自 app/agents/tools.py）
# ============================================================
_COLLOQUIAL_CACHE: dict | None = None


def _load_colloquial_map() -> dict:
    global _COLLOQUIAL_CACHE
    if _COLLOQUIAL_CACHE is not None:
        return _COLLOQUIAL_CACHE
    mapping = {}
    if _COLLOQUIAL_FILE.exists():
        with open(_COLLOQUIAL_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("口语化") or line.startswith("==="):
                    continue
                parts = line.split("\t", 1)
                if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                    mapping[parts[0].strip()] = parts[1].strip()
    _COLLOQUIAL_CACHE = mapping
    return mapping


def map_colloquial_to_legal(query: str) -> str:
    """口语表述替换为专业术语（长匹配优先，只替换第一个）"""
    items = sorted(_load_colloquial_map().items(), key=lambda x: -len(x[0]))
    for colloquial, legal in items:
        if colloquial in query:
            return query.replace(colloquial, legal, 1)
    return query

# ============================================================
# ③ 数据库检索（pg_trgm 宽召回 + 关键词补充 + 无结果重试）
# ============================================================
def _dedup(candidates: list, heading: str, content: str, sim: float) -> bool:
    if any(e["heading"] == heading and e["content"][:50] == content[:50] for e in candidates):
        return False
    candidates.append({"heading": heading, "content": content[:500], "similarity": sim})
    return True


def _ngram_keywords(query: str, max_words: int = 20) -> list:
    """查询拆成 2-4 字滑动窗口子词（去重，长词优先，控制数量）

    动机：短口语查询（如"离婚财产怎么分"）整句 ILIKE 命中不了长法条，
    拆成"离婚""财产"等子词后显著提升召回。
    """
    text = re.sub(r"[，。？?！!、\s]+", "", query)
    words = set()
    for n in range(4, 1, -1):
        for i in range(len(text) - n + 1):
            words.add(text[i:i + n])
    # 长词优先（信息量更大），限制数量防查询过慢
    return sorted(words, key=lambda x: -len(x))[:max_words]


async def _recall(conn, query: str, top_k: int) -> list:
    """召回：相似度排序（阈值滤噪）+ 子词 ILIKE 补充（始终执行）

    调优依据（2026-08 实测）：
    - `content % $1` 对短查询相似度 ≈ 0（6 字查询对长法条 trigram 相似度 0.0000）；
      对映射后的长句又会返回一批"含共同子词"的噪音条款占满配额。
    - 因此改为：全量按相似度排序、低于 0.1 的噪音直接丢弃；ILIKE 子词补充无条件执行。
    """
    recall_limit = max(top_k * 4, 20)

    # 1) pg_trgm 相似度排序召回（0.1 以下视为噪音丢弃——短查询与长法条的
    #    trigram 相似度实测为 0.0000~0.075，真正的命中靠 ILIKE 子词）
    rows = await conn.fetch(
        "SELECT heading, content, similarity(content, $1) AS sim "
        "FROM knowledge_chunks WHERE source = 'civil_code' "
        "ORDER BY sim DESC LIMIT $2",
        query, recall_limit * 2,
    )
    candidates = []
    for r in rows:
        sim = r["sim"] or 0
        if sim < 0.1:
            break  # 已按相似度降序，后续只会更低
        candidates.append({"heading": r["heading"], "content": r["content"][:500],
                           "similarity": round(sim, 4)})

    # 2) 子词 ILIKE 补充（无条件执行；dedup 保证不重复）
    for kw in _ngram_keywords(query):
        if len(candidates) >= recall_limit:
            break
        more = await conn.fetch(
            "SELECT heading, content FROM knowledge_chunks "
            "WHERE source = 'civil_code' AND content ILIKE $1 LIMIT $2",
            f"%{kw}%", recall_limit - len(candidates),
        )
        for r in more:
            _dedup(candidates, r["heading"], r["content"], 0.5)
    return candidates

# ============================================================
# ④ DeepSeek Rerank 精排（仅低置信时触发，省 token）
# ============================================================
async def _rerank_with_deepseek(query: str, candidates: list, top_k: int) -> list:
    api_key = get("DEEPSEEK_API_KEY")
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY not set")
    items_text = "\n\n".join(
        f"[{i + 1}] {c['heading']}\n{c['content'][:300]}"
        for i, c in enumerate(candidates)
    )
    prompt = (
        "你是一个法律知识检索重排序专家。请判断以下候选段落与用户查询的相关性。\n\n"
        f"用户查询：{query}\n\n候选段落：\n{items_text}\n\n"
        "请对每个候选段落给出相关性评分（0-10分，10分最相关），"
        '只返回JSON格式的评分数组，不要其他文字。格式：{"scores": [分数1, 分数2, ...]}'
    )
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{DEEPSEEK_API_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": DEEPSEEK_MODEL, "messages": [{"role": "user", "content": prompt}],
                  "response_format": {"type": "json_object"}, "temperature": 0.1, "max_tokens": 1024},
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        scores = __import__("json").loads(content).get("scores", [])
    if len(scores) != len(candidates):
        raise ValueError(f"评分数量({len(scores)})与候选数({len(candidates)})不匹配")
    for i, c in enumerate(candidates):
        c["rerank_score"] = round(scores[i], 2) if isinstance(scores[i], (int, float)) else 0
    candidates.sort(key=lambda x: x["rerank_score"], reverse=True)
    return candidates[:top_k]

# ============================================================
# MCP Tool（唯一对外能力：语义清晰的原子动作）
# ============================================================
@mcp.tool()
async def search_civil_code(query: str, top_k: int = 5, use_rerank: bool = True) -> dict:
    """检索《中华人民共和国民法典》知识库，返回相关法条与解释。

    内部流程：法律误区守卫 → 口语术语映射 → 关键词+相似度双通道召回 → 可选 Rerank 精排。
    若问题属于其他法律领域（如消保法），返回纠正引导而非检索结果。

    Args:
        query: 用户的法律问题或检索关键词。
        top_k: 返回结果条数，默认 5。
        use_rerank: 是否启用 LLM 精排（默认 True；False 时仅按相似度排序，省 token）。
    """
    # ① 法律误区守卫（命中直接返回引导，不检索）
    try:
        hit = check_law_mapping(query)
        if hit:
            return {
                "results": [{"heading": "该问题不属于民法典调整范围", "content": hit["message"],
                             "similarity": 1.0, "law": hit["law"], "citation": hit["citation"]}],
                "method": "law_mapping", "mapping_hit": True,
            }
    except Exception:
        pass  # 守卫失败不影响正常检索

    # ② 口语→术语映射（映射后的查询更利于 pg_trgm）
    mapped = map_colloquial_to_legal(query)

    import asyncpg
    url = db_url("localhost")
    try:
        conn = await asyncpg.connect(url, timeout=10)
        try:
            # ③ 召回
            candidates = await _recall(conn, mapped, top_k)
            if not candidates and mapped != query:
                candidates = await _recall(conn, query, top_k)  # 无结果时回退原查询
            if not candidates:
                return {"results": [], "method": "pg_trgm", "mapping_hit": False}
        finally:
            await conn.close()
    except Exception as e:
        return {"error": f"数据库检索失败: {e}", "method": "error", "mapping_hit": False}

    # ④ 精排策略：高置信直接截取（省 token），低置信才调 Rerank
    top_sim = max(c["similarity"] for c in candidates)
    if use_rerank and top_sim < RERANK_SIM_THRESHOLD:
        try:
            candidates = await _rerank_with_deepseek(query, candidates, top_k)
            method = "pg_trgm+rerank"
        except Exception:
            candidates = sorted(candidates, key=lambda x: x["similarity"], reverse=True)[:top_k]
            method = "pg_trgm"
    else:
        candidates = sorted(candidates, key=lambda x: x["similarity"], reverse=True)[:top_k]
        method = "pg_trgm"

    return {"results": candidates, "method": method, "mapping_hit": False,
            "query_mapped": mapped if mapped != query else None}


# ============================================================
# Resources（可读数据：让模型知道"我能查什么"）
# ============================================================
@mcp.resource("civil://coverage")
async def kb_coverage() -> str:
    """知识库覆盖说明：民法典各编法条数量与检索能力边界。

    模型在回答前可读取此资源，判断知识库是否能支撑当前问题。
    """
    import asyncpg
    try:
        conn = await asyncpg.connect(db_url("localhost"), timeout=10)
        try:
            rows = await conn.fetch(
                "SELECT heading FROM knowledge_chunks WHERE source='civil_code'")
        finally:
            await conn.close()
    except Exception as e:
        return f"知识库不可达: {e}"

    from collections import Counter
    stats = Counter()
    for r in rows:
        m = re.match(r"第[一二三四五六七八九十百千万]+编", r["heading"])
        stats[m.group(0) if m else "未知"] += 1
    lines = [f"- {k}：{v} 条法条" for k, v in sorted(stats.items())]
    return (f"《中华人民共和国民法典》知识库（共 {len(rows)} 条法条，按编/章/条分块存储）\n"
            + "\n".join(lines)
            + "\n\n检索说明：支持关键词+相似度双通道召回与 LLM 精排；"
              "若问题属于其他法律领域（如消保法），检索会返回纠正引导而非法条。")


@mcp.resource("civil://law_mapping")
def law_mapping_table() -> str:
    """法律误区映射表：常见口语问法 → 正确法律依据（防止模型答错法域）。"""
    if not _MAPPING_FILE.exists():
        return "映射表文件不存在"
    lines = []
    for entry in _load_law_mapping():
        lines.append(f"- {entry['scenario']} → {entry['law']}（{entry['citation']}）")
    return ("以下问题常被误归入《民法典》，实际由其他法律调整：\n"
            + "\n".join(lines))


# ============================================================
# Prompts（可复用模板：帮助 Host 生成更有效的调用）
# ============================================================
@mcp.prompt()
def legal_query_rewriter(question: str) -> str:
    """将口语化法律问题改写为适合知识库检索的专业表述。

    Args:
        question: 用户原始口语问题，如"离婚财产怎么分"。
    """
    return (
        "你是法律检索助手。请把用户的口语化法律问题改写为适合知识库检索的"
        "专业法律术语查询（保留关键法条概念，去除口语表达），只输出改写结果。\n"
        f"用户问题：{question}"
    )


if __name__ == "__main__":
    mcp.run()
