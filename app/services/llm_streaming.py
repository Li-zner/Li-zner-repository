"""统一流式原语：节流推送 / 模型链重试 / 空回复兜底 / DFA 安全过滤

抽取自 app/routes/v2.py（2026-09 重构，行为等价纯移动）。
节流规则：首块立即推送，后续 ≥40 字符 或 ≥100ms 冲刷；流尾冲刷残余缓冲。
"""
import time
from typing import AsyncIterator, List, Dict

from ..core.config import DEEPSEEK_FALLBACK_MESSAGE, DEEPSEEK_FLASH_MODEL, DEEPSEEK_MODEL
from ..core.logging import setup_logging
from ..core.stream_utils import stream_llm
from ..core.semantic_cache import SemanticCache
from ..core.safety_filter import get_filter
from .chat_support import mark_key_result
from .chat_stream_ctx import ChatStreamCtx

logger = setup_logging()


async def stream_llm_throttled(
    ctx: ChatStreamCtx, api_key: str, model_try: str, llm_messages: List[Dict],
    username: str = "", tools: Dict = None, tool_choice: str = None,
    temperature: float = None, max_tokens: int = None,
) -> AsyncIterator[tuple]:
    """调用 stream_llm 并按「首块立即 + ≥40字符/≥100ms + 尾冲刷」节流产出事件

    产出 (kind, payload)：
      - ("reasoning", text)  思考文本（未过滤，由调用方做 sanitize/hide）
      - ("answer", chunk)    节流后的回答块
      - ("usage", dict)      token 用量
      - ("tool_calls", {"delta": [..]})  工具调用增量
    """
    _stream_buffer = ""
    _first_chunk_time = None
    _last_chunk_time = None
    async for _ev in stream_llm(
        api_key, model_try, llm_messages,
        tools=tools, tool_choice=tool_choice, username=username,
        temperature=temperature, max_tokens=max_tokens,
    ):
        if _ev["type"] == "usage":
            yield ("usage", {"prompt_tokens": _ev["prompt_tokens"], "completion_tokens": _ev["completion_tokens"]})
        elif _ev["type"] == "reasoning":
            yield ("reasoning", _ev["text"])
        elif _ev["type"] == "content":
            _chunk = _ev["text"]
            _stream_buffer += _chunk
            _now = time.time()
            if _first_chunk_time is None:
                _first_chunk_time = _now
                _last_chunk_time = _now
                yield ("answer", _chunk)
                _stream_buffer = ""
            elif len(_stream_buffer) >= 40 or (_now - _last_chunk_time >= 0.1 and _stream_buffer):
                yield ("answer", _stream_buffer)
                _stream_buffer = ""
                _last_chunk_time = _now
        elif _ev["type"] == "tool_calls":
            yield ("tool_calls", {"delta": _ev["delta"]})
    # 流尾冲刷残余缓冲（原版简单/推荐通道会丢弃 <40 字符的残余；统一实现改为冲刷，不丢内容）
    if _stream_buffer:
        yield ("answer", _stream_buffer)


async def answer_via_models(ctx: ChatStreamCtx, llm_messages: List[Dict], tag: str) -> AsyncIterator[tuple]:
    """按 model_try_list 顺序调用模型并把节流事件透传给调用方

    单模型失败标记 Key 后换下一个；全部失败/空回复由调用方兜底。
    产出 (kind, text) 供快速通道渲染 reasoning/answer。
    """
    for _model_try in ctx.model_try_list:
        try:
            _result_text = ""
            async for kind, payload in stream_llm_throttled(
                ctx, ctx.api_key, _model_try, llm_messages, username=ctx.username,
            ):
                if kind == "reasoning":
                    yield ("reasoning", payload)
                elif kind == "answer":
                    _result_text += payload
                    yield ("answer", payload)
            if _result_text:
                return
        except Exception as _fast_err:
            await mark_key_result(ctx.api_key, False)
            if _model_try == ctx.selected_model:
                logger.warning(f"{tag} {ctx.selected_model} 调用失败，降级到 {DEEPSEEK_FLASH_MODEL}: {_fast_err}")
            else:
                logger.warning(f"{tag} {DEEPSEEK_FLASH_MODEL} 也失败: {_fast_err}")
    # 全部模型失败：交由调用方 ensure_answer 兜底


async def ensure_answer(ctx: ChatStreamCtx, result_text: str, fallback_msg: str, write_cache: bool = False) -> str:
    """空回复兜底 + 穿透占位；返回最终答案文本"""
    if not result_text:
        result_text = DEEPSEEK_FALLBACK_MESSAGE
        logger.warning(f"{fallback_msg}")
        await mark_key_result(ctx.api_key, False)
        # 穿透防护：底层失败 → 写入短 TTL 空值占位（await 确保写入，P0 #11）
        await SemanticCache.set_empty(ctx.req.query, cache_ctx=ctx.cache_ctx, ttl=30)
    return result_text


def apply_safety_filter(text: str) -> str:
    """DFA 敏感词过滤（命中即替换为安全提示）"""
    sf = get_filter()
    if sf.contains_sensitive(text):
        return sf.safe_message
    return text
