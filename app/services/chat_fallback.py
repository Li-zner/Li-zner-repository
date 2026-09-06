"""对话降级链：主模型失败后的 Flash 备胎（单级降级，Dify 已彻底摒弃）

抽取自 app/routes/v2.py（2026-09 重构，行为等价纯移动）。
"""
import json
from typing import AsyncIterator

import httpx

from ..models.schemas import ChatRequest
from ..core.config import (
    DEEPSEEK_API_BASE, DEEPSEEK_FALLBACK_MESSAGE,
    DEEPSEEK_FLASH_MODEL, HTTP_TIMEOUT_MEDIUM,
)
from ..core.concurrency import llm_semaphore
from ..core.jfast import loads as jloads
from ..core.logging import setup_logging
from ..core.semantic_cache import SemanticCache
from ..core.stream_utils import sse
from .chat_support import get_deepseek_key, lang_instruction, mark_key_result

logger = setup_logging()


async def fallback_flash(req: ChatRequest, username: str) -> AsyncIterator[str]:
    """第一级降级：使用 DeepSeek Flash（无工具调用，纯聊天）"""
    from ..core.metrics import gateway_errors_total
    gateway_errors_total.labels(status='fallback_flash').inc()
    logger.info(f"触发 Flash 降级: username={username}")
    deepseek_api_key = await get_deepseek_key()
    if not deepseek_api_key:
        logger.warning("Flash 降级无 API Key，跳过")
        return
    try:
        async with llm_semaphore:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_MEDIUM) as client:
                async with client.stream(
                    "POST",
                    f"{DEEPSEEK_API_BASE}/chat/completions",
                    headers={"Authorization": f"Bearer {deepseek_api_key}", "Content-Type": "application/json"},
                    json={
                        "model": DEEPSEEK_FLASH_MODEL,
                        "messages": [
                            {"role": "system", "content": "你是AI助手，请直接回答用户的问题。如果涉及法律问题，回答相关法律规定。如果无法查询知识库，请用已有知识回答。" + lang_instruction(req)},
                            {"role": "user", "content": req.query}
                        ],
                        "user": username,
                        "stream": True,
                        "temperature": 0.3,
                        "max_tokens": 2048,
                    }
                ) as resp:
                    resp.raise_for_status()
                    await mark_key_result(deepseek_api_key, True)
                    async for line in resp.aiter_lines():
                        if line.startswith("data:"):
                            data_str = line[5:].strip()
                            if data_str == "[DONE]":
                                yield "data: [DONE]\n\n"
                                return
                            try:
                                data = jloads(data_str)
                                chunk = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                                if chunk:
                                    # ===== DFA 敏感词过滤 =====
                                    from ..core.safety_filter import get_filter
                                    sf = get_filter()
                                    result = sf.check_stream(chunk)
                                    if not result['safe']:
                                        logger.warning(f"DFA 拦截Flash降级: 敏感词='{result['triggered_word']}'")
                                        yield sse("answer_chunk", sf.safe_message)
                                        yield sse("answer_complete", sf.safe_message) + "data: [DONE]\n\n"
                                        return
                                    # ============================
                                    yield sse("answer_chunk", chunk)
                            except (json.JSONDecodeError, ValueError) as e:
                                logger.debug(f"SSE 行解析失败跳过: {e}")
                    # 流正常结束但未收到 [DONE]（兜底）
                    yield "data: [DONE]\n\n"
    except Exception as e:
        await mark_key_result(deepseek_api_key, False)
        logger.warning(f"Flash 降级失败: {e}")


async def fallback_chain(req: ChatRequest, username: str, cache_ctx: str = "") -> AsyncIterator[str]:
    """降级链：仅 Flash（Dify 已移除）

    兜底失败时写入短 TTL 空值占位防穿透风暴（await 而非 create_task，
    确保写入完成，P0 #11 防击穿占位不悬空）。
    """
    yielded = False
    try:
        async for chunk in fallback_flash(req, username):
            yielded = True
            yield chunk
    except Exception:
        # P3 修复：保留 yielded 真值——Flash 已产出部分内容时不再叠加兜底文案（原实现
        # 重置 yielded 会造成"部分内容 + 兜底文案"先后发出）；补 [DONE] 终结事件防前端悬挂
        # （Flash 中途异常路径自身不发 [DONE]）
        if yielded:
            yield "data: [DONE]\n\n"
    # 兜底：主模型 + Flash 双双失败时，保证至少返回一个终结事件，避免空流（前端一直转圈）
    if not yielded:
        # 穿透防护：兜底失败 → 写入短 TTL 空值占位，避免后续请求持续打 LLM
        try:
            await SemanticCache.set_empty(req.query, cache_ctx=cache_ctx, ttl=30)
        except Exception as e:
            logger.debug(f"穿透占位写入失败（TTL 自愈）: {e}")
        yield sse("answer_complete", DEEPSEEK_FALLBACK_MESSAGE) + "data: [DONE]\n\n"
