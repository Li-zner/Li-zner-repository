"""RAG 修复动作的内置处理器：健康探测、诊断重跑、精确清缓存。

2026-09-16 从 rag_actions.py 拆出，保持单文件 600 行上限。处理器只做只读
或可回滚操作；健康探测的白名单与 SSRF 限制集中在 _validate_probe_url。
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

_DEFAULT_PROBE_TARGETS = {
    "gateway": "http://agent_gateway:10086/health",
    "console": "http://rag_console:13150/ready",
    "worker": "http://rag_console_worker:13151/ready",
}
_PROBE_ALLOWED_HOSTS = {
    host.strip().lower()
    for host in os.getenv(
        "RAG_PROBE_ALLOWED_HOSTS",
        "agent_gateway,rag_console,rag_console_worker",
    ).split(",")
    if host.strip()
}


def _probe_targets() -> dict[str, str]:
    """读取健康探测白名单；环境变量仅覆盖同名目标，不开放任意 URL。"""
    targets = dict(_DEFAULT_PROBE_TARGETS)
    raw = os.getenv("RAG_PROBE_TARGETS", "").strip()
    if not raw:
        return targets
    for item in raw.split(","):
        name, separator, url = item.partition("=")
        if separator and name.strip() and url.strip():
            targets[name.strip().lower()] = url.strip()
    return targets


def _validate_probe_url(url: str) -> str:
    """限制探测协议、路径、认证信息和查询串，阻断 SSRF 扩展面。"""
    parsed = urlsplit(url)
    if parsed.scheme != "http" or not parsed.hostname:
        raise ValueError("健康探测仅允许 http 内网地址")
    if parsed.hostname.lower() not in _PROBE_ALLOWED_HOSTS:
        raise ValueError("健康探测主机不在白名单")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("健康探测 URL 不允许认证信息、查询串或片段")
    if parsed.path not in ("/health", "/ready"):
        raise ValueError("健康探测路径仅允许 /health 或 /ready")
    return url


def _resolve_probe_url(params: dict) -> tuple[str, str]:
    """按目标名解析白名单地址；显式 url 只能复述同一白名单值。"""
    targets = _probe_targets()
    target = str(params.get("target") or "gateway").strip().lower()
    if target not in targets:
        raise ValueError(f"未授权的健康探测目标: {target}")
    url = _validate_probe_url(targets[target])
    supplied = str(params.get("url") or "").strip()
    if supplied and supplied != url:
        raise ValueError("健康探测 url 与目标白名单不一致")
    return target, url


async def _probe_health(params: dict) -> dict:
    """只读健康探测，不修改任何运行状态。"""
    import httpx

    target, url = _resolve_probe_url(params)
    async with httpx.AsyncClient(
            timeout=5.0, follow_redirects=False, trust_env=False) as client:
        response = await client.get(url)
    return {
        "result": {
            "target": target,
            "status_code": response.status_code,
            "url": url,
        },
        "verification": {"passed": response.status_code == 200},
    }


async def _rerun_diagnosis(_params: dict) -> dict:
    """立即触发一轮规则诊断。"""
    from .rag_monitor import run_cycle

    count = await run_cycle()
    return {
        "result": {"new_verdicts": count},
        "verification": {"passed": True},
    }


async def _clear_exact_cache(params: dict) -> dict:
    """精确清除指定 query 与上下文的一条缓存，不影响其他用户。"""
    query = str(params.get("query") or "").strip()
    cache_ctx = str(params.get("cache_ctx") or "")
    if not query:
        raise ValueError("clear_exact_cache 缺少 query")
    from ..core.semantic_cache import SemanticCache

    deleted = await SemanticCache.invalidate_exact(query, cache_ctx)
    return {
        "result": {"deleted": deleted},
        "verification": {"passed": True, "deleted": deleted},
    }
