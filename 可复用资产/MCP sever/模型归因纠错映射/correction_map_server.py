"""
MCP 资产 005 — 模型归因纠错映射 Server（stdio 传输）

从 agent_gateway law_mapping.py（法律依据纠正映射表）泛化：模型有顽固错误归因时，
改 prompt 无效，用「LLM 前拦截的映射表」架构手段绕过（踩坑清单 #27，CC019 2分→7分）。

能力：
  1. lookup_redirect —— 文本命中映射则返回权威答案/引导（exact 优先，再 contains）
  2. add_mapping     —— 新增/更新映射（幂等持久化到 JSON 文件）
  3. list_mappings   —— 列出全部映射
  4. remove_mapping  —— 删除映射

零第三方依赖（除 mcp 本身），数据文件 JSON 原子写（临时文件 + replace）。

运行（stdio）：
    python correction_map_server.py
纯逻辑自检（无需任何外部服务）：
    python correction_map_server.py --self-check
验证 Client：
    python test_client.py
"""
import json
import sys
import threading
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("correction-map")

_DATA_FILE = Path(__file__).resolve().parent / "data" / "mappings.json"
_lock = threading.Lock()
_mappings: list = []  # [{trigger, answer, mode: "exact"|"contains"}]


def _load() -> None:
    global _mappings
    try:
        _mappings = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _mappings = []


def _save() -> None:
    _DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _DATA_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_mappings, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_DATA_FILE)  # 原子替换，防写一半损坏


_load()


@mcp.tool()
async def lookup_redirect(text: str) -> dict:
    """查询映射：命中返回权威答案/引导，未命中返回 miss。

    匹配优先级：exact（完全一致）> contains（包含，按列表顺序先注册先得）。

    Args:
        text: 用户原始输入（LLM 调用前先查这个表）。
    """
    if not text or not text.strip():
        return {"error": "text 不能为空"}
    with _lock:
        for m in _mappings:
            if m["mode"] == "exact" and text == m["trigger"]:
                return {"hit": True, "mode": "exact", "trigger": m["trigger"],
                        "answer": m["answer"]}
        for m in _mappings:
            if m["mode"] == "contains" and m["trigger"] in text:
                return {"hit": True, "mode": "contains", "trigger": m["trigger"],
                        "answer": m["answer"]}
    return {"hit": False}


@mcp.tool()
async def add_mapping(trigger: str, answer: str, mode: str = "contains") -> dict:
    """新增/更新映射（同 trigger+mode 已存在则覆盖 answer，幂等）。

    Args:
        trigger: 触发词（如 "七天无理由退货"）。
        answer: 权威答案或引导话术（如 "该场景适用《消费者权益保护法》..."）。
        mode: "exact" 完全一致才触发；"contains" 包含即触发（默认）。
    """
    if not trigger or not answer:
        return {"error": "trigger 与 answer 不能为空"}
    if mode not in ("exact", "contains"):
        return {"error": "mode 只能是 exact 或 contains"}
    with _lock:
        for m in _mappings:
            if m["trigger"] == trigger and m["mode"] == mode:
                m["answer"] = answer
                _save()
                return {"updated": True, "trigger": trigger, "mode": mode}
        _mappings.append({"trigger": trigger, "answer": answer, "mode": mode})
        _save()
    return {"added": True, "trigger": trigger, "mode": mode}


@mcp.tool()
async def list_mappings() -> dict:
    """列出全部映射（用于排查/审计/展示）。"""
    with _lock:
        return {"total": len(_mappings), "mappings": list(_mappings)}


@mcp.tool()
async def remove_mapping(trigger: str, mode: str = "contains") -> dict:
    """删除映射（按 trigger+mode 精确匹配）。

    Args:
        trigger: 触发词。
        mode: 与添加时一致的模式。
    """
    with _lock:
        for i, m in enumerate(_mappings):
            if m["trigger"] == trigger and m["mode"] == mode:
                removed = _mappings.pop(i)
                _save()
                return {"removed": True, "trigger": removed["trigger"]}
    return {"removed": False, "reason": "未找到该映射"}


def _self_check() -> None:
    """纯逻辑自检：exact/contains 匹配、优先级、幂等更新、持久化往返（用临时文件）。"""
    global _DATA_FILE, _mappings
    import tempfile
    _DATA_FILE = Path(tempfile.mkdtemp()) / "mappings.json"  # 隔离，不动真实数据
    _mappings = []
    import asyncio
    asyncio.run(_check_core())
    print("全部自检通过")


async def _check_core() -> None:
    # 1. contains 命中
    r = await add_mapping("七天无理由退货", "适用《消费者权益保护法》", "contains")
    assert r["added"] is True
    r = await lookup_redirect("请问七天无理由退货可以吗")
    assert r["hit"] and "消费者权益保护法" in r["answer"] and r["mode"] == "contains"
    print("[self-check] contains 命中 OK")

    # 2. exact 优先级高于 contains（同 trigger 两种模式都存在时）
    await add_mapping("离婚", "完整离婚场景答案", "exact")
    r = await lookup_redirect("离婚")
    assert r["hit"] and r["mode"] == "exact"
    print("[self-check] exact 优先 OK")

    # 3. miss
    r = await lookup_redirect("今天天气如何")
    assert r["hit"] is False
    print("[self-check] miss OK")

    # 4. 幂等更新：同 trigger+mode 覆盖不新增
    await add_mapping("七天无理由退货", "新答案", "contains")
    assert len(_mappings) == 2
    r = await lookup_redirect("七天无理由退货")
    assert r["answer"] == "新答案"
    print("[self-check] 幂等更新 OK")

    # 5. 持久化往返：重载文件后数据一致
    _load()
    assert len(_mappings) == 2
    print("[self-check] 持久化往返 OK")


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        _self_check()
    else:
        mcp.run()
