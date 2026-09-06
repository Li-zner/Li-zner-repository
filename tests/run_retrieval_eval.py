"""
民法典检索质量专项评测 — recall@k / MRR / 延迟 / 检索方法分布

定位（AI 层补强指南 A.5）：RAGAS faithfulness 测的是"回答忠于检索结果"，
检索本身漏没漏它测不出来。本脚本只评检索层，是 RAG 升级（向量路/RRF/rerank）
前后对比的硬证据。

用法（需 DATABASE_URL 指向含 knowledge_chunks 的 PG）：
    python tests/run_retrieval_eval.py

标注来源 tests/civil_retrieval_labels.json：法条号取自 test_cases.yaml 的
expected_keywords（作者整理的期望答案）+ 3 条人工补充；命中判定 = 法条号
出现在检索块的 heading+content 中（heading 是编/章级标题，法条号在 content 开头）。
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import yaml

BASE = Path(__file__).resolve().parent.parent

# 从 .env 加载（与 run_civil_eval.py 同一模式）
_env_path = BASE / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

TOP_K = 20          # 评 recall@20 的最大窗口
KS = (5, 10, 20)    # recall 截断点


def load_cases_and_labels() -> tuple[list[dict], dict, dict]:
    """载入 60 用例与标注，返回 (评测项, excluded, 一致性告警列表)"""
    raw = yaml.safe_load((BASE / "tests" / "test_cases.yaml").read_text(encoding="utf-8"))
    all_cases = raw.get("test_cases", raw)
    civil = [c for c in all_cases if c.get("category") == "民法典"]
    label_doc = json.loads((BASE / "tests" / "civil_retrieval_labels.json").read_text(encoding="utf-8"))
    labels: dict = label_doc.get("labels", {})
    excluded: dict = label_doc.get("excluded", {})

    warnings = []
    yaml_ids = {c["id"] for c in civil}
    for cid in labels:
        if cid not in yaml_ids:
            warnings.append(f"标注 id 不在用例集: {cid}")
    for cid in excluded:
        if cid not in yaml_ids:
            warnings.append(f"排除 id 不在用例集: {cid}")
    unlabeled = yaml_ids - set(labels) - set(excluded)
    if unlabeled:
        warnings.append(f"既无标注也未排除的用例: {sorted(unlabeled)}")

    items = [
        {"id": c["id"], "query": c["query"], "articles": labels.get(c["id"], [])}
        for c in civil if c["id"] in labels
    ]
    return items, excluded, warnings


def _chunk_text(chunk: dict) -> str:
    """检索块的命中语料：heading + content（法条号在 content 开头，heading 是编/章）"""
    return f"{chunk.get('heading', '')}\n{chunk.get('content', '')}"


def score_case(results: list[dict], articles: list[str]) -> dict:
    """单用例指标：recall@k（按条文平均）+ MRR（首个命中任一条文的结果排名倒数）"""
    recall = {}
    for k in KS:
        window = [_chunk_text(c) for c in results[:k]]
        hits = sum(1 for a in articles if any(a in t for t in window))
        recall[k] = hits / len(articles) if articles else 0.0
    mrr = 0.0
    for rank, chunk in enumerate(results, 1):
        text = _chunk_text(chunk)
        if any(a in text for a in articles):
            mrr = 1.0 / rank
            break
    return {"recall": recall, "mrr": mrr}


async def run_eval() -> dict:
    """跑全部标注用例，返回报告 dict（含逐用例明细，供前后对比 diff）"""
    import logging
    logging.getLogger().setLevel(logging.WARNING)   # 收敛导入侧 INFO 日志，保持报告可读
    sys.path.insert(0, str(BASE))
    from app.core.db import close_pool, init_pool
    from app.agents.tools import search_knowledge

    await init_pool()   # 脚本脱离 app lifespan，需显式建池

    items, excluded, warnings = load_cases_and_labels()
    for w in warnings:
        print(f"[标注告警] {w}")

    details, methods, latencies = [], {}, []
    for it in items:
        t0 = time.perf_counter()
        try:
            res = await search_knowledge(it["query"], top_k=TOP_K, permissions=[])
            err = None
        except Exception as e:
            res, err = None, f"{type(e).__name__}: {e}"
        dt_ms = round((time.perf_counter() - t0) * 1000)

        if err:
            details.append({"id": it["id"], "error": err})
            print(f"  {it['id']} ERROR {err[:80]}")
            continue
        if res.get("mapping_hit"):
            # 法律纠正映射表直接拦截（如消保法问题）：不经检索，单独归类
            details.append({"id": it["id"], "category": "mapping_hit"})
            continue
        results = res.get("results", [])
        method = res.get("method", "?")
        methods[method] = methods.get(method, 0) + 1
        latencies.append(dt_ms)
        score = score_case(results, it["articles"])
        got = [(c.get("heading") or "")[:24] for c in results[:3]]
        details.append({
            "id": it["id"], "category": "retrieved", "method": method,
            "latency_ms": dt_ms, **score, "top3_heading": got,
        })
        flag = "OK " if score["recall"][10] >= 0.5 else "MISS"
        print(f"  {it['id']} {flag} recall@10={score['recall'][10]:.2f} "
              f"mrr={score['mrr']:.2f} {dt_ms}ms [{method}]")

    await close_pool()
    return summarize(details, methods, latencies, excluded)


def summarize(details, methods, latencies, excluded) -> dict:
    """聚合宏平均指标与低分清单（分数跌 3% 即应回滚对应改造）"""
    scored = [d for d in details if d.get("category") == "retrieved"]
    by_id = {d["id"]: d for d in scored}
    report = {
        "evaluated": len(scored),
        "mapping_hit": sum(1 for d in details if d.get("category") == "mapping_hit"),
        "errors": sum(1 for d in details if "error" in d),
        "excluded_count": len(excluded),
        "excluded": excluded,
        "methods": methods,
        "latency_ms": {
            "avg": round(sum(latencies) / len(latencies)) if latencies else 0,
            "p95": sorted(latencies)[int(len(latencies) * 0.95)] if latencies else 0,
        },
        "detail": details,
    }
    for k in KS:
        vals = [by_id[d["id"]]["recall"][k] for d in scored]
        report[f"recall@{k}"] = round(sum(vals) / len(vals), 4) if vals else 0.0
    mrr_vals = [by_id[d["id"]]["mrr"] for d in scored]
    report["mrr"] = round(sum(mrr_vals) / len(mrr_vals), 4) if mrr_vals else 0.0
    report["zero_hit_at_10"] = [
        d["id"] for d in scored if d["recall"][10] == 0.0
    ]
    return report


async def main():
    if not os.getenv("DATABASE_URL"):
        raise SystemExit("缺少 DATABASE_URL 环境变量（指向含 knowledge_chunks 的 PG）")
    print(f"民法典检索评测：{TOP_K} 召回窗口，截断点 {KS}")
    report = await run_eval()
    out = BASE / "tests" / "retrieval_eval_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    print()
    print("=" * 56)
    print(f"评测完成：检索 {report['evaluated']} | 映射表拦截 {report['mapping_hit']}"
          f" | 错误 {report['errors']} | 排除 {report['excluded_count']}")
    for k in KS:
        print(f"  recall@{k:<3}= {report[f'recall@{k}']:.4f}")
    print(f"  MRR      = {report['mrr']:.4f}")
    print(f"  延迟 avg/p95 = {report['latency_ms']['avg']}ms / {report['latency_ms']['p95']}ms")
    print(f"  检索方法分布 = {report['methods']}")
    if report["zero_hit_at_10"]:
        print(f"  recall@10 零命中（优先分析）: {report['zero_hit_at_10']}")
    print(f"明细报告: {out}")
    print("=" * 56)


if __name__ == "__main__":
    asyncio.run(main())
