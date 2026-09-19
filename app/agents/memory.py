import asyncio
import httpx
from ..core.logging import setup_logging
from ..core.config import (
    DEEPSEEK_MODEL,
    HTTP_TIMEOUT_MEDIUM,
    apply_llm_request_options,
    llm_endpoint,
)

logger = setup_logging()


def compress_message_history(messages: list, max_messages: int = 6) -> list:
    """把较早的对话历史压成一条简短摘要，保留最近几轮上下文。"""
    if not messages:
        return messages
    if len(messages) <= max_messages:
        return messages

    # 已有的【历史摘要】system 消息（来自滚动压缩）不参与截断，始终保留
    summaries = [m for m in messages
                 if m.get("role") == "system" and (m.get("content") or "").startswith("【历史摘要】")]
    rest = [m for m in messages if m not in summaries]
    # 保护主 system（人格/规则提示词）：它不是对话历史，被压进摘要会让 LLM 失去人设与规则约束。
    # runner 会对包含主 system 的完整消息列表调用本函数，历史列表无主 system 时此分支不生效
    lead_system = []
    if rest and rest[0].get("role") == "system" and not (rest[0].get("content") or "").startswith("【历史摘要】"):
        lead_system = [rest[0]]
        rest = rest[1:]
    if len(rest) <= max_messages:
        return messages

    prior_messages = rest[:-max_messages]
    recent_messages = rest[-max_messages:]
    # 切点对齐：不得把 assistant(tool_calls) 与其 tool 消息切开——孤儿 tool 消息会让
    # 下一次 LLM 请求 400（OpenAI 协议要求每个 tool_call_id 都有对应 tool 消息）。
    # recent 开头是 tool 时向左扩到配对的 assistant。
    while recent_messages and recent_messages[0].get("role") == "tool" and prior_messages:
        recent_messages.insert(0, prior_messages.pop())
    if not prior_messages:
        return messages
    prior_text_parts = []
    for msg in prior_messages:
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        prior_text_parts.append(content[:120])

    summary_text = " ".join(prior_text_parts[-4:]) if prior_text_parts else "历史上下文较多"
    compacted = lead_system + summaries + [{
        "role": "system",
        "content": f"【历史摘要】请忽略更早的冗长对话，只按以下要点继续回答：{summary_text[:600]}"
    }]
    compacted.extend(recent_messages)
    return compacted


async def generate_summary(messages: list, max_tokens: int = 300, username: str = "") -> str:
    """使用 DeepSeek 生成对话摘要（带指数退避重试，防网络抖动导致压缩静默失效）

    username 供扣费（2026-09-09 主人拍板：内部 LLM 调用计入计费）。
    """
    if not messages:
        return ""
    recent = messages[-50:] if len(messages) > 50 else messages
    conversation_text = "\n".join([
        f"{msg['role']}: {msg.get('content', '')}" for msg in recent if msg.get('content')
    ])
    prompt = f"""
请用中文为以下对话生成一段简洁的摘要（不超过{max_tokens}字），概括用户的主要需求、目的地、人数、预算等关键信息，以便后续对话能继续理解上下文。

对话内容：
---
{conversation_text}
---
摘要：
"""
    from ..services.chat_support import get_deepseek_key
    api_key = await get_deepseek_key()
    if not api_key:
        return ""

    # 主力模型可能是 qwen（百炼端点），按模型名路由端点与密钥
    base_url, api_key = llm_endpoint(DEEPSEEK_MODEL, api_key)
    # 指数退避重试（最多 3 次）：超时/抖动/空响应都不再让摘要功能静默失效（P1/P2）
    from ..core.concurrency import llm_semaphore
    for attempt in range(3):
        try:
            # 纳入全局 LLM 并发闸（2026-09-10 审查 P2 对齐 orchestrator 同修法）
            async with llm_semaphore:
                async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_MEDIUM) as client:
                    resp = await client.post(
                        f"{base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json=apply_llm_request_options({
                            "model": DEEPSEEK_MODEL,
                            "messages": [{"role": "user", "content": prompt}],
                            "temperature": 0.3,
                            "max_tokens": max_tokens
                        }, DEEPSEEK_MODEL)
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    # 计入计费（2026-09-09 主人拍板）：记忆压缩属用户请求触发的 LLM 消耗
                    _usage = data.get("usage") or {}
                    if username and (_usage.get("prompt_tokens") or _usage.get("completion_tokens")):
                        from ..services.llm_streaming import record_token_usage
                        record_token_usage(_usage, username, "")
                    summary = data["choices"][0]["message"]["content"].strip()
                    if summary:
                        return summary
                _err = "空摘要"
        except Exception as e:
            _err = str(e)[:120]
        # 空摘要与异常同样退避（0.5s、1s），防立即重打（P2 修复：退避原先只在异常分支）
        if attempt < 2:
            await asyncio.sleep(0.5 * (2 ** attempt))
        logger.warning(f"生成摘要失败（第{attempt+1}次）: {_err}")
    return ""
