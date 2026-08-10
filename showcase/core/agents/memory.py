import json
import os
import httpx
from ..core.redis import get_redis
from ..core.logging import setup_logging
from ..core.config import DEEPSEEK_API_BASE, DEEPSEEK_MODEL, HTTP_TIMEOUT_MEDIUM

logger = setup_logging()


def compress_message_history(messages: list, max_messages: int = 6) -> list:
    """把较早的对话历史压成一条简短摘要，保留最近几轮上下文。"""
    if not messages:
        return messages
    if len(messages) <= max_messages:
        return messages

    prior_messages = messages[:-max_messages]
    recent_messages = messages[-max_messages:]
    prior_text_parts = []
    for msg in prior_messages:
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        prior_text_parts.append(content[:120])

    summary_text = " ".join(prior_text_parts[-4:]) if prior_text_parts else "历史上下文较多"
    compacted = [{
        "role": "system",
        "content": f"【历史摘要】请忽略更早的冗长对话，只按以下要点继续回答：{summary_text[:600]}"
    }]
    compacted.extend(recent_messages)
    return compacted


async def generate_summary(messages: list, max_tokens: int = 300) -> str:
    """使用 DeepSeek 生成对话摘要"""
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
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        return ""
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_MEDIUM) as client:
            resp = await client.post(
                f"{DEEPSEEK_API_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": DEEPSEEK_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                    "max_tokens": max_tokens
                }
            )
            resp.raise_for_status()
            data = resp.json()
            summary = data["choices"][0]["message"]["content"].strip()
            return summary
    except Exception as e:
        logger.warning(f"生成摘要失败: {e}")
        return ""