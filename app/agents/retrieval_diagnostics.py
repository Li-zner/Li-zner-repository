"""检索候选阶段快照；仅供离线评测按金标条文做漏斗归因。"""
from __future__ import annotations

import os


def diagnostics_enabled() -> bool:
    """默认关闭，避免生产请求保留额外候选列表。"""
    return os.getenv("KB_DIAGNOSTICS_ENABLED", "0") == "1"


def chunk_keys(items: list | None) -> list[str]:
    """提取候选块键并保持阶段内原始顺序。"""
    return [
        str(item.get("chunk_key"))
        for item in (items or [])
        if item.get("chunk_key")
    ]
