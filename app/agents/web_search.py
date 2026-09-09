"""联网搜索工具（duckduckgo_search 主路径 + Instant Answer 降级）

2026-09-07 自 tools.py 拆出（tools.py 超 600 行硬限，按子系统边界切分）；
tools.py 保留 re-export，外部 import 路径不变。行为等价纯移动。
"""
import asyncio
import time

import httpx

from ..core.config import HTTP_TIMEOUT_MEDIUM
from ..core.logging import setup_logging

logger = setup_logging()


# web_search 本地限流（P1 #13/#40：防高频调用导致外部搜索 API 封 IP）
# 按 user_key 独立计数（P1：改全局限流为用户级，避免多用户并发互相误伤）。
_search_rate_lock = asyncio.Lock()
_search_rate_state: dict = {}   # user_key -> (window_start, count)
_SEARCH_WINDOW_SECONDS = 10.0
_SEARCH_MAX_PER_WINDOW = 10
# 计数字典键数上限：超过即触发惰性清扫（防每用户一个键无界增长，P2 修复）
_SEARCH_STATE_MAX_KEYS = 512


async def _check_search_rate(user_key: str = ""):
    """web_search 本地限流：每 user_key 每 10 秒最多 10 次。

    user_key 缺省为 ""（未透传用户时降到全局兜底），透传 username 后按用户隔离。
    键数超阈值时惰性清扫已过窗口的旧计数（活跃用户的窗口未过期不受影响）。
    """
    key = user_key or "_global"
    async with _search_rate_lock:
        now = time.time()
        if len(_search_rate_state) > _SEARCH_STATE_MAX_KEYS:
            expired = [k for k, (ws, _c) in _search_rate_state.items()
                       if now - ws > _SEARCH_WINDOW_SECONDS]
            for k in expired:
                del _search_rate_state[k]
        window_start, count = _search_rate_state.get(key, (0.0, 0))
        if now - window_start > _SEARCH_WINDOW_SECONDS:
            window_start, count = now, 0
        if count >= _SEARCH_MAX_PER_WINDOW:
            raise RuntimeError("搜索过于频繁，请稍后再试")
        _search_rate_state[key] = (window_start, count + 1)


async def web_search(query: str, max_results: int = 5, user_key: str = ""):
    """
    联网搜索工具 — 当用户询问实时信息、营业时间、评价、排队情况等
    现有工具无法覆盖的内容时调用。

    主路径：duckduckgo_search 的 DDGS 是纯同步客户端（8.x 只导出 DDGS，
    没有 AsyncDDGS/atext），阻塞调用放 asyncio.to_thread 执行；
    未安装/无结果/异常时降级 httpx 直连 Instant Answer API。
    user_key 为调用方用户名，用于按用户限流（缺省走全局兜底）。
    """
    await _check_search_rate(user_key)  # 本地限流（P1 #13/#40）
    try:
        from duckduckgo_search import DDGS

        def _ddg_text() -> list:
            # 同步阻塞搜索放线程池，避免卡住事件循环
            with DDGS() as ddgs:
                return ddgs.text(query, region='cn-zh', max_results=max_results) or []

        raw = await asyncio.to_thread(_ddg_text)
        results = [{
            "title": r.get("title", ""),
            "body": r.get("body", "")[:500],
            "href": r.get("href", ""),
        } for r in raw]
        if results:
            logger.info(f"联网搜索完成: query={query[:30]}, results={len(results)}")
            return {"results": results, "total": len(results)}
        logger.info("DDGS 无结果，降级 Instant Answer")
    except ImportError:
        logger.warning("duckduckgo_search 未安装，降级 Instant Answer")
    except Exception as e:
        logger.warning(f"DDGS 搜索失败，降级 Instant Answer: {e}")

    return await _instant_answer(query)


async def _instant_answer(query: str) -> dict:
    """降级：DuckDuckGo Instant Answer API（通常只返回一条摘要，搜索能力弱于主路径）"""
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_MEDIUM) as client:
            resp = await client.get(
                "https://api.duckduckgo.com/",
                params={"q": query, "format": "json", "no_html": "1"},
            )
            data = resp.json()
        results = []
        abstract = data.get("AbstractText", "")
        if abstract:
            results.append({
                "title": data.get("Heading", "摘要"),
                "body": abstract[:500],
                "href": data.get("AbstractURL", ""),
            })
        for topic in data.get("RelatedTopics", [])[:3]:
            if "Text" in topic:
                results.append({
                    "title": topic.get("Text", "")[:100],
                    "body": topic.get("Text", "")[:500],
                    "href": topic.get("FirstURL", ""),
                })
        return {"results": results, "total": len(results)}
    except Exception as e:
        # httpx 部分异常 str 为空，补类型名保证日志可排查
        logger.error(f"联网搜索失败: {type(e).__name__}: {e}")
        # 异常 str 可能带含 key 的完整 URL，前端只给类型名（细节已进日志）
        return {"error": type(e).__name__, "results": [], "total": 0}
