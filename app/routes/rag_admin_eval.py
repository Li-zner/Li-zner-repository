"""RAG 中控台测评视图（仅管理员）—— 把离线测评产物暴露成只读接口。

背景（2026-09-18）：检索与端到端测评此前只能在命令行跑、在 JSON 文件里翻，
中控台只能看到 batch5 那一份顶层聚合指标（`rag_admin_status._load_latest_evaluation`）。
本模块把 `app/rag_eval/reports/` 下的多份测评报告列出来并给出逐条明细，
让"这次跑了哪些测评集、每一道题命中在第几位"可以直接在中控台看到。

口径说明：
- 报告文件随 `COPY app/` 进镜像 —— `Dockerfile.rag_console` 有意排除 `tests/*.json`，
  所以报告要展示必须先归集到 `app/rag_eval/reports/`；
- **只读、不落库**：这些是测试产物而非运行时数据，落库只会制造第二份真相；
- 摘要**优先取报告自身的顶层口径**，只有顶层缺失的字段（如 recall@10 / MRR）
  才从 `detail` 聚合，以免中控台另算一套与报告不一致的数字；
- 明细丢弃 `diagnostics`（逐条几十个 chunk key，前端用不上）。

挂载方式：本模块自带 `eval_router`（不带 prefix），由 `rag_admin.rag_admin_router`
用 `include_router` 挂载，前缀 `/api/admin/rag` 由父 router 提供
（与 `rag_admin_changesets` 同一形态，避免 URL/注册顺序漂移）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from ..middleware.auth import get_current_user

eval_router = APIRouter(tags=["admin"])

_REPORTS_DIR = Path(__file__).resolve().parents[1] / "rag_eval" / "reports"
# 只接受纯文件名的 .json：既挡住路径穿越，也挡住子目录
_REPORT_NAME = re.compile(r"^[A-Za-z0-9_.\-]{1,120}\.json$")


def _avg(values: list) -> float | None:
    """求平均并保留 4 位；无有效数值时返回 None（不拿 0 冒充）。"""
    nums = [v for v in values if isinstance(v, (int, float))]
    if not nums:
        return None
    return round(sum(nums) / len(nums), 4)


# 映射表直命中 / 主动排除的条目没有检索指标；混进均值等于把"没跑检索"当成"检索得 0 分"
_NON_RETRIEVAL_CATEGORIES = {"mapping_hit", "excluded"}


def _scored_rows(detail: list) -> list:
    """筛出真正经过检索、带 recall/mrr 的明细行，口径与报告自称的 evaluated 对齐。

    实证：all190 两份报告的 `detail` 有 190 条，其中 6 条 `category=mapping_hit`
    （被法律纠正映射表直接命中，未走检索），而 `evaluated` 是 184 —— 正好是
    `retrieved` 的条数。所以均值必须按此筛选，不能对全量明细求平均。
    """
    return [row for row in detail
            if row.get("category") not in _NON_RETRIEVAL_CATEGORIES]


def _report_path(name: str) -> Path:
    """把报告名解析成受控目录下的真实文件，越界或不存在一律 404。"""
    if not _REPORT_NAME.fullmatch(name):
        raise HTTPException(status_code=404, detail="测评报告不存在")
    path = _REPORTS_DIR / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="测评报告不存在")
    return path


def _load_report(path: Path) -> dict:
    """读取报告 JSON；不可读或非对象一律 503，不返回半个结构。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=503, detail=f"测评报告不可读: {type(exc).__name__}")
    if not isinstance(data, dict):
        raise HTTPException(status_code=503, detail="测评报告结构不可识别")
    return data


def _kind_of(data: dict) -> str:
    """按结构识别报告类型：检索测评 / 端到端评分 / 其他。"""
    if isinstance(data.get("detail"), list):
        return "retrieval"
    if isinstance(data.get("results"), list):
        return "e2e"
    return "raw"


def _summarize_retrieval(data: dict, detail: list) -> dict:
    """检索测评摘要：顶层有就用顶层，没有的才从明细聚合（口径见 `_scored_rows`）。"""
    rows = _scored_rows(detail)
    latency = data.get("latency_ms") or {}
    return {
        "evaluated": data.get("evaluated", len(rows)),
        "detail_count": len(detail),
        "mapping_hit_count": len(detail) - len(rows),
        "split": data.get("split"),
        "errors": data.get("errors", 0),
        "methods": data.get("methods"),
        "recall_at_5": _avg([(d.get("recall") or {}).get("5") for d in rows]),
        "recall_at_10": _avg([(d.get("recall") or {}).get("10") for d in rows]),
        "mrr": _avg([d.get("mrr") for d in rows]),
        "zero_hit_at_10": [
            d.get("id") for d in rows
            if (d.get("recall") or {}).get("10") == 0
        ],
        "latency_ms": {"avg": latency.get("avg"), "p95": latency.get("p95")},
        "excluded_count": data.get("excluded_count", 0),
    }


def _summarize_e2e(data: dict, results: list) -> dict:
    """端到端评分摘要：按 RAG 判分统计，并保留报告自己的结论性文字。"""
    verdicts: dict[str, int] = {}
    for item in results:
        key = str(item.get("rag_verdict") or "unknown")
        verdicts[key] = verdicts.get(key, 0) + 1
    return {
        "evaluated": len(results),
        "split": None,
        "errors": 0,
        "methods": None,
        "verdicts": verdicts,
        "note": data.get("note"),
        "net_gain": data.get("net_gain"),
        "regression": data.get("regression"),
    }


def _retrieval_items(detail: list) -> list:
    """检索明细精简版：只留前端要渲染的列，丢掉 diagnostics。"""
    items = []
    for row in detail:
        recall = row.get("recall") or {}
        items.append({
            "id": row.get("id"),
            "category": row.get("category"),
            "method": row.get("method"),
            "latency_ms": row.get("latency_ms"),
            "recall_at_5": recall.get("5"),
            "recall_at_10": recall.get("10"),
            "recall_at_20": recall.get("20"),
            "mrr": row.get("mrr"),
            "top3_heading": row.get("top3_heading") or [],
        })
    return items


def _e2e_items(results: list) -> list:
    """端到端明细：保留答案全文（条数有限），由前端决定是否展开。"""
    items = []
    for row in results:
        answer = row.get("rag_answer") or ""
        items.append({
            "id": row.get("id"),
            "group": row.get("group"),
            "naive_verdict": row.get("naive_verdict"),
            "rag_verdict": row.get("rag_verdict"),
            "chunks_used": row.get("chunks_used"),
            "answer_len": len(answer),
            "answer": answer,
        })
    return items


def _summarize(data: dict, kind: str) -> dict:
    """按类型分派摘要计算。"""
    if kind == "retrieval":
        return _summarize_retrieval(data, data.get("detail") or [])
    if kind == "e2e":
        return _summarize_e2e(data, data.get("results") or [])
    return {"evaluated": 0, "split": None, "errors": 0, "methods": None,
            "keys": sorted(data.keys())}


def _list_reports() -> list:
    """扫描报告目录，最新的排前面；读不了的报告跳过而不是整体失败。"""
    if not _REPORTS_DIR.is_dir():
        return []
    paths = sorted(
        _REPORTS_DIR.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True)
    items = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        kind = _kind_of(data)
        items.append({
            "report": path.name,
            "kind": kind,
            "size_bytes": path.stat().st_size,
            "modified_at": datetime.fromtimestamp(
                path.stat().st_mtime).isoformat(timespec="seconds"),
            "summary": _summarize(data, kind),
        })
    return items


def _require_admin(current_user: dict) -> None:
    """与本模块之外的只读接口保持同一权限口径。"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")


@eval_router.get("/evaluations")
async def rag_evaluations(current_user: dict = Depends(get_current_user)):
    """列出全部离线测评报告及其摘要（管理员）。"""
    _require_admin(current_user)
    return {"items": _list_reports()}


@eval_router.get("/evaluations/{report}")
async def rag_evaluation_detail(
        report: str,
        current_user: dict = Depends(get_current_user)):
    """读取单份测评报告的摘要与逐条明细（管理员）。"""
    _require_admin(current_user)
    path = _report_path(report)
    data = _load_report(path)
    kind = _kind_of(data)
    detail = data.get("detail") or []
    results = data.get("results") or []
    return {
        "report": path.name,
        "kind": kind,
        "modified_at": datetime.fromtimestamp(
            path.stat().st_mtime).isoformat(timespec="seconds"),
        "summary": _summarize(data, kind),
        "items": _retrieval_items(detail) if kind == "retrieval"
        else _e2e_items(results) if kind == "e2e" else [],
    }
