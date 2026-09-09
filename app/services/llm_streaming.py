"""统一流式原语：节流推送 / 模型链重试 / 空回复兜底 / DFA 安全过滤

抽取自 app/routes/v2.py（2026-09 重构，行为等价纯移动）。
节流规则：首块立即推送，后续 ≥40 字符 或 ≥100ms 冲刷；流尾冲刷残余缓冲。
"""
import time
import asyncio
from typing import AsyncIterator, List, Dict

from ..core.config import DEEPSEEK_FALLBACK_MESSAGE, DEEPSEEK_FLASH_MODEL, DEEPSEEK_MODEL
from ..core.metrics import llm_tokens_total, llm_tokens_detail, llm_requests_total
from ..core.logging import setup_logging
from ..core.stream_utils import stream_llm
from ..core.semantic_cache import SemanticCache
from ..core.safety_filter import get_filter
from ..core.concurrency import spawn
from ..payment.service import deduct_token_cost
from .chat_support import mark_key_result
from .reasoning_guard import ReasoningStreamGuard
from .chat_stream_ctx import ChatStreamCtx

logger = setup_logging()


async def stream_llm_throttled(
    ctx: ChatStreamCtx, api_key: str, model_try: str, llm_messages: List[Dict],
    username: str = "", tools: Dict = None, tool_choice: str = None,
    temperature: float = None, max_tokens: int = None,
) -> AsyncIterator[tuple]:
    """调用 stream_llm 并按「首块立即 + ≥40字符/≥100ms + 尾冲刷」节流产出事件

    产出 (kind, payload)：
      - ("reasoning", text)  思考文本（已经 ReasoningStreamGuard 过滤：系统词/模型
                             身份/Key 指纹不外露，跨块拼接检测，强指纹整流抑制）
      - ("answer", chunk)    节流后的回答块
      - ("usage", dict)      token 用量
      - ("tool_calls", {"delta": [..]})  工具调用增量
    """
    _stream_buffer = ""
    _first_chunk_time = None
    _last_chunk_time = None
    _reason_guard = None  # 惰性创建：仅出现思考增量的流才需要
    async for _ev in stream_llm(
        api_key, model_try, llm_messages,
        tools=tools, tool_choice=tool_choice, username=username,
        temperature=temperature, max_tokens=max_tokens,
    ):
        if _ev["type"] == "usage":
            yield ("usage", {"prompt_tokens": _ev["prompt_tokens"], "completion_tokens": _ev["completion_tokens"]})
        elif _ev["type"] == "reasoning":
            if _reason_guard is None:
                _reason_guard = ReasoningStreamGuard(getattr(ctx, "persona_id", ""))
            _safe_text = _reason_guard.feed(_ev["text"])
            if _safe_text:
                yield ("reasoning", _safe_text)
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


def _log_deduct_task_error(task: asyncio.Task) -> None:
    """扣费后台任务的异常认领：记 warning，不打断对话，也不留未认领的 Task 异常"""
    if not task.cancelled() and task.exception():
        logger.warning(f"Token扣费后台任务失败（不影响对话）: {task.exception()}")


def bill_token_usage(usage: dict, username: str, session_id: str, remark: str):
    """日 token 计数 + 钱包扣费（不含 Prometheus 指标，供已自行打点的路径复用）

    2026-09-09 审查 P0：agent 任务路径（runner）原先只打 v2_task 指标、从不扣费
    也不计日 token（"旁路漏扣费"历史教训同类），复用本函数补齐且避免指标双计。
    扣费任务统一走 spawn（强引用防 GC 中途回收 → 静默漏扣，2026-09-07 审查 P2）。
    """
    prompt_tk = usage.get("prompt_tokens", 0)
    completion_tk = usage.get("completion_tokens", 0)
    if not (prompt_tk or completion_tk):
        return
    # 日 token 计数（2026-09-07 审查 P1）：daily_token 限额唯一写入方；
    # 日期用 UTC（与 ensure_chat_allowed 门禁读取同一日切口径）
    from datetime import datetime, timezone
    from ..middleware.rate_limit import update_daily_usage
    total_tk = prompt_tk + completion_tk
    _today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    spawn(update_daily_usage(username, _today, inc_request=0, inc_token=total_tk),
          name="daily-token-incr")
    spawn(deduct_token_cost(
        user_id=username, token_count=total_tk, session_id=session_id or "",
        remark=remark,
    ), name="token-deduct").add_done_callback(_log_deduct_task_error)


def _estimate_tokens(text: str) -> int:
    """断连估算兜底：拿不到上游 usage 时按已产出文本粗估。

    中文≈1字1token、英文≈4字符1token，混合取 2 字符 1 token 折中。
    ponytail: 粗估偏差可达 ±50%，仅断连兜底用；正常路径始终走真实 usage 事件。
    """
    if not text:
        return 0
    return max(1, len(text) // 2)


def _spawn_drain_bill(agen, username: str, conv_id: str, tag: str,
                      fallback_text: str = "") -> None:
    """断连排空计费（2026-09-09 审查 P1 修复：断连漏扣费）。

    客户端断开时下游生成器收到 GeneratorExit/CancelledError，此刻上游流对象
    （stream_llm → httpx resp）尚未关闭——排空任务接管消费直至上游自然结束，
    拿到真实 usage 事件后按真实值补计费；上游中途失败则按已产出文本估算兜底。
    ponytail: 排空会把上游剩余输出消耗完（费用与用户不断连时一致），换取计量准确；
    spawn 持强引用防 GC 中途回收。
    """
    async def _drain():
        usage = None
        try:
            async for kind, payload in agen:
                if kind == "usage":
                    usage = payload
        except Exception as e:
            logger.warning(f"{tag} 断连排空中断（{type(e).__name__}），退化估算计费")
            usage = None
        if usage and (usage.get("prompt_tokens") or usage.get("completion_tokens")):
            record_token_usage(usage, username, conv_id)
            logger.info(f"{tag} 断连排空完成，按真实 usage 补计费")
        elif fallback_text:
            est = _estimate_tokens(fallback_text)
            record_token_usage(
                {"prompt_tokens": 0, "completion_tokens": est},
                username, conv_id,
                remark=f"断连估算补计费（{est} tokens，按已产出文本粗估）")
            logger.info(f"{tag} 上游未给出 usage，按估算补计费 {est} tokens")

    spawn(_drain(), name=f"drain-bill:{tag}")


def record_token_usage(_stream_usage: dict, username: str, conv_id: str, remark: str = ""):
    """Token 精细计量 + 扣费（10元/万token，模拟模式）

    Prometheus 指标 + 日 token 计数 + 异步扣费；扣费失败经 done_callback 记 warning，不影响对话主流程。
    2026-09-06 自 chat_react 下沉至本模块：快速通道（answer_via_models）此前不透传
    usage 事件导致主流量漏扣费，现由透传点统一计量，ReAct/快速通道两路径共用。
    remark 供断连估算等特殊计费路径标注口径。
    """
    from ..core.config import DEEPSEEK_MODEL as _dm
    prompt_tk = _stream_usage.get("prompt_tokens", 0)
    completion_tk = _stream_usage.get("completion_tokens", 0)
    if prompt_tk or completion_tk:
        llm_tokens_total.labels(type='input').inc(prompt_tk)
        llm_tokens_total.labels(type='output').inc(completion_tk)
        llm_tokens_detail.labels(model=_dm, endpoint='v2_chat', type='input').inc(prompt_tk)
        llm_tokens_detail.labels(model=_dm, endpoint='v2_chat', type='output').inc(completion_tk)
    llm_requests_total.labels(model=_dm, endpoint='v2_chat', status='success').inc()
    bill_token_usage(_stream_usage, username, conv_id,
                     remark=remark or f"AI对话消耗 {prompt_tk + completion_tk} tokens（输入 {prompt_tk} + 输出 {completion_tk}）")


async def answer_via_models(ctx: ChatStreamCtx, llm_messages: List[Dict], tag: str) -> AsyncIterator[tuple]:
    """按 model_try_list 顺序调用模型并把节流事件透传给调用方

    单模型失败标记 Key 后换下一个；全部失败/空回复由调用方兜底。
    产出 (kind, text) 供快速通道渲染 reasoning/answer。
    """
    for _model_try in ctx.model_try_list:
        _result_text = ""
        _agen = stream_llm_throttled(
            ctx, ctx.api_key, _model_try, llm_messages, username=ctx.username,
        )
        try:
            async for kind, payload in _agen:
                if kind == "reasoning":
                    yield ("reasoning", payload)
                elif kind == "answer":
                    _result_text += payload
                    yield ("answer", payload)
                elif kind == "usage":
                    record_token_usage(payload, ctx.username, ctx.conv_id)
            if _result_text:
                return
        except (GeneratorExit, asyncio.CancelledError):
            # 客户端断连（2026-09-09 审查 P1 修复：断连漏扣费）：此刻上游流未关闭，
            # 排空任务接管消费拿真实 usage 补计费后原样上抛，不吞取消
            _spawn_drain_bill(_agen, ctx.username, ctx.conv_id,
                              f"fast-path[{_model_try}]", fallback_text=_result_text)
            raise
        except Exception as _fast_err:
            await mark_key_result(ctx.api_key, False)
            if _model_try == ctx.selected_model:
                logger.warning(f"{tag} {ctx.selected_model} 调用失败，降级到 {DEEPSEEK_FLASH_MODEL}: {_fast_err}")
            else:
                logger.warning(f"{tag} {DEEPSEEK_FLASH_MODEL} 也失败: {_fast_err}")
    # 全部模型失败：交由调用方 ensure_answer 兜底


async def ensure_answer(ctx: ChatStreamCtx, result_text: str, fallback_msg: str) -> str:
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
