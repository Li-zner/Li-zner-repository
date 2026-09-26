"""检索质量快速门禁：固定用例集的变更前后对比判定。

定位：L1/L2 修复动作在应用变更后，用同一套固定用例复测，指标退化则回滚。
口径：复用 tests/run_retrieval_eval.py 的打分与聚合，保证门禁结论与完整评测可比。

用例集为什么是这 20 条
--------------------
45 条 holdout 里 39 条 recall@5/@10 双满分，改动轻微退化时它们不动，检不出问题。
因此按"敏感度分层"挑 20 条，让门禁对召回、排序、窗口三类退化都有探针：

- 召回敏感：CC099(0.00/1.00)、CC061/065/098(0.50/1.00)、CC076(0.67/1.00)
- 召回@10 硬探针：CC150(0.67/0.67)，全量 holdout 中唯一 @10 不满分的用例
- 排序（MRR）敏感：CC070(0.50)、CC079(0.33)、CC080/090(0.25)，recall 满分但首位靠后
- 底线：9 条满分用例，按 CC 编号分段铺开，防整体崩溃而非细微退化

基线（2026-09-16，`RERANK_TOP=15` + `text-embedding-v4@768` 定稿配置）：
20 条子集 recall@5 ≈ 0.83、recall@10 1.00、MRR ≈ 0.83。
实际值以首次 `run_gate()` 落盘的基线文件为准，不要手抄。
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from functools import lru_cache
from pathlib import Path

GATE_CASE_IDS: tuple[str, ...] = (
    # 召回敏感层
    "CC061", "CC065", "CC076", "CC098", "CC099",
    # 召回@10 硬探针
    "CC150",
    # MRR 敏感层
    "CC070", "CC079", "CC080", "CC090",
    # 底线层（按编号分段）
    "CC063", "CC068", "CC074", "CC083", "CC093",
    "CC108", "CC120", "CC128", "CC138", "CC149",
)

# 门槛：完整 holdout 发布门禁用文档 8.3 的「相对下降 3%」口径；
# 快速门禁只有 20 条，宏平均抖动大，改用绝对门槛降低误报。
# ponytail: 样本量小是原理性天花板，20 条只能挡住明显退化，发布前仍须跑 45 条 holdout。
GATE_RECALL10_DROP_MAX = 0.05     # recall@10 绝对下降上限
GATE_MRR_REL_DROP_MAX = 0.05      # MRR 相对下降上限
GATE_P95_RATIO_MAX = 1.5          # p95 延迟相对基线上限

_EVAL_SCRIPT = Path(__file__).resolve().parents[2] / "tests" / "run_retrieval_eval.py"


@lru_cache(maxsize=1)
def _eval_module():
    """按文件路径加载评测脚本，避免为 app 侧调用给 tests 强行加包结构。"""
    if not _EVAL_SCRIPT.is_file():
        raise FileNotFoundError(f"评测脚本缺失: {_EVAL_SCRIPT}")
    spec = importlib.util.spec_from_file_location("run_retrieval_eval", _EVAL_SCRIPT)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载评测脚本: {_EVAL_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def run_gate() -> dict:
    """在当前进程配置下跑固定门禁用例，返回与完整评测同构的报告。"""
    module = _eval_module()
    report = await module.run_eval(split="holdout", only_ids=set(GATE_CASE_IDS))
    report["gate_case_count"] = len(GATE_CASE_IDS)
    return report


def _pct_drop(baseline: float, after: float) -> float:
    """相对下降比例；基线为 0 时返回 0，避免除零放大成假告警。"""
    if not baseline:
        return 0.0
    return (baseline - after) / baseline


def compare_gate(baseline: dict, after: dict) -> dict:
    """按门槛比较前后报告，返回 passed 与逐条退化原因。"""
    reasons: list[str] = []
    evaluated = int(after.get("evaluated") or 0)
    base_evaluated = int(baseline.get("evaluated") or 0)

    if evaluated < len(GATE_CASE_IDS) or base_evaluated < len(GATE_CASE_IDS):
        # 样本不足不得判成功（文档 8.3：样本不足转人工观察）
        reasons.append(
            f"样本不足：基线 {base_evaluated} 条、复测 {evaluated} 条，"
            f"低于门禁要求的 {len(GATE_CASE_IDS)} 条"
        )
        return {
            "passed": False,
            "insufficient_sample": True,
            "reasons": reasons,
            "baseline": gate_summary(baseline),
            "after": gate_summary(after),
        }

    after_errors = int(after.get("errors") or 0)
    if after_errors > int(baseline.get("errors") or 0):
        reasons.append(f"检索报错增加：{after_errors} 条")

    after_zero = set(after.get("zero_hit_at_10") or [])
    base_zero = set(baseline.get("zero_hit_at_10") or [])
    new_zero = sorted(after_zero - base_zero)
    if new_zero:
        reasons.append(f"新增零命中（recall@10=0）：{new_zero}")

    recall10_drop = float(baseline.get("recall@10") or 0) - float(
        after.get("recall@10") or 0)
    if recall10_drop > GATE_RECALL10_DROP_MAX:
        reasons.append(
            f"recall@10 下降 {recall10_drop:.4f}，超过上限 {GATE_RECALL10_DROP_MAX}"
        )

    mrr_drop = _pct_drop(float(baseline.get("mrr") or 0), float(after.get("mrr") or 0))
    if mrr_drop > GATE_MRR_REL_DROP_MAX:
        reasons.append(
            f"MRR 相对下降 {mrr_drop:.2%}，超过上限 {GATE_MRR_REL_DROP_MAX:.0%}"
        )

    base_p95 = float((baseline.get("latency_ms") or {}).get("p95") or 0)
    after_p95 = float((after.get("latency_ms") or {}).get("p95") or 0)
    if base_p95 and after_p95 > base_p95 * GATE_P95_RATIO_MAX:
        reasons.append(
            f"p95 延迟 {after_p95:.0f}ms 超过基线的 {GATE_P95_RATIO_MAX} 倍"
            f"（基线 {base_p95:.0f}ms）"
        )

    return {
        "passed": not reasons,
        "insufficient_sample": False,
        "reasons": reasons,
        "baseline": gate_summary(baseline),
        "after": gate_summary(after),
    }


def gate_summary(report: dict) -> dict:
    """抽取比对关心的指标，避免逐用例明细进入动作证据字段。"""
    return {
        "evaluated": report.get("evaluated"),
        "recall@5": report.get("recall@5"),
        "recall@10": report.get("recall@10"),
        "mrr": report.get("mrr"),
        "zero_hit_at_10": report.get("zero_hit_at_10"),
        "errors": report.get("errors"),
        "latency_ms": report.get("latency_ms"),
    }


async def _main() -> int:
    parser = argparse.ArgumentParser(description="检索质量门禁")
    parser.add_argument("--out", default="", help="把本次报告写入该 JSON 路径")
    parser.add_argument("--baseline", default="",
                        help="基线报告路径；给了则输出对比结论并以退出码表达")
    parser.add_argument("--full", action="store_true",
                        help="跑完整 holdout（45 条，发布前门禁）；默认跑 20 条快速门禁")
    args = parser.parse_args()

    sys.path.insert(0, str(_EVAL_SCRIPT.parents[1]))
    if args.full:
        report = await _eval_module().run_eval(split="holdout")
    else:
        report = await run_gate()
    summary = gate_summary(report)
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if args.out:
        Path(args.out).write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        verdict = compare_gate(baseline, report)
        print(json.dumps({"passed": verdict["passed"],
                          "reasons": verdict["reasons"]},
                         ensure_ascii=False, indent=2))
        return 0 if verdict["passed"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
