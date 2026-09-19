"""联网搜索工具（duckduckgo_search 主路径 + Instant Answer 降级）

2026-09-07 自 tools.py 拆出（tools.py 超 600 行硬限，按子系统边界切分）；
tools.py 保留 re-export，外部 import 路径不变。行为等价纯移动。
"""
import asyncio

import httpx

from ..core.config import HTTP_TIMEOUT_MEDIUM
from ..core.logging import setup_logging
from ..core.redis import get_redis

logger = setup_logging()


# web_search 分布式限流（P1 #13/#40：防高频调用导致外部搜索 API 封 IP）
_SEARCH_WINDOW_SECONDS = 10.0
_SEARCH_MAX_PER_WINDOW = 10
_SEARCH_RATE_LUA = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
if current > tonumber(ARGV[2]) then
    return 0
end
return 1
"""


async def _check_search_rate(user_key: str = ""):
    """web_search 分布式限流：每 user_key 每 10 秒最多 10 次。"""
    key = user_key or "_global"
    redis = await get_redis()
    allowed = await redis.eval(
        _SEARCH_RATE_LUA, 1, f"search:rate:{key}",
        int(_SEARCH_WINDOW_SECONDS), _SEARCH_MAX_PER_WINDOW,
    )
    if allowed != 1:
        raise RuntimeError("搜索过于频繁，请稍后再试")


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
                # 显式超时（2026-09-10 审查 P2）：不依赖库默认值，与全局超时口径一致
                return ddgs.text(query, region='cn-zh', max_results=max_results,
                                 timeout=10) or []

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
