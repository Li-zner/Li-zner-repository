"""
流式通道公共工具层 — 消除 v2.py / runner.py 三通道重复

抽取自 app/routes/v2.py 的重复模式（2026-08 重构，行为等价纯移动）：
  1. dispatch_tool   —— 工具名 → 执行结果（原 3 处 if/elif 分发链）
  2. build_file_context —— 上传文件注入（原 3 处"读 Redis → 组装"重复）
  3. sanitize_uploaded_content —— 上传内容防注入过滤（原 v2.py 本地函数）
  4. stream_llm      —— DeepSeek 流式调用生成器（原 v2.py×3 + runner.py×1 重复）
  5. sse             —— SSE 事件格式化（原 19 处手写 data: JSON）
"""
import json
import re

import httpx

from ..agents.sub_agents import call_sub_agent
from ..agents.tools import (
    fetch_weather_async, search_knowledge,
    search_project_knowledge, web_search,
)
from ..core.config import (
    DEEPSEEK_API_BASE, DEEPSEEK_API_TIMEOUT, LLM_CONNECT_TIMEOUT, LLM_TEMPERATURE,
)
from ..core.constants import CITIES
from ..core.concurrency import llm_semaphore
from ..core.jfast import loads as jloads
from ..core.redis import get_redis


def sse(event: str, content) -> str:
    """SSE 事件行格式化：{"type": event, "content": content}

    ensure_ascii=True 与原实现（v2.py 手写 json.dumps）行为一致——中文转义为
    \\uXXXX（前端已验证的传输路径）；不要改为 False。
    """
    return f"data: {json.dumps({'type': event, 'content': content})}\n\n"


async def stream_llm(api_key: str, model: str, messages: list, *,
                     tools: list | None = None, tool_choice: str = "auto",
                     temperature: float | None = LLM_TEMPERATURE, max_tokens: int | None = 2048,
                     username: str | None = None):
    """DeepSeek 流式调用生成器 — yield 统一事件 dict（调用方决定消费方式）

    事件类型：
      {"type": "reasoning", "text": str}       思考内容增量
      {"type": "content",   "text": str}       回答内容增量
      {"type": "tool_calls", "delta": list}    工具调用流式片段（需调用方累积）
      {"type": "usage", "prompt_tokens": int, "completion_tokens": int}  用量（出现一次）

    temperature/max_tokens 传 None 时不加入请求体（保持模型默认，用于 ReAct 循环路径）。
    上游失败抛异常，由调用方决定降级策略（不在此吞异常）。
    """
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice
    if username:
        payload["user"] = username

    async with llm_semaphore:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(DEEPSEEK_API_TIMEOUT, connect=LLM_CONNECT_TIMEOUT)
        ) as client:
            async with client.stream(
                "POST",
                f"{DEEPSEEK_API_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        data = jloads(data_str)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    try:
                        delta = data.get("choices", [{}])[0].get("delta", {})
                    except IndexError:
                        # 兼容 usage-only chunk（choices 为空数组）：仅取 usage，无 delta
                        delta = {}
                    if "usage" in data:
                        u = data["usage"]
                        yield {"type": "usage",
                               "prompt_tokens": u.get("prompt_tokens") or 0,
                               "completion_tokens": u.get("completion_tokens") or 0}
                    if delta.get("reasoning_content"):
                        yield {"type": "reasoning", "text": delta["reasoning_content"]}
                    if delta.get("content"):
                        yield {"type": "content", "text": delta["content"]}
                    if delta.get("tool_calls"):
                        yield {"type": "tool_calls", "delta": delta["tool_calls"]}


def sanitize_uploaded_content(content: str) -> str:
    """安全过滤上传文件内容，防止提示词注入（原 v2.py _sanitize_uploaded_content）

    注（C5）：单次替换为轻量防御，理论上替换后仍可能组合成新注入；
    完整防护需多重迭代替换/完全过滤可疑字符，当前风险可接受。
    """
    if not content:
        return content
    injection_patterns = [
        r'(?i)忽略(之前|以上|前面).*?指令',
        r'(?i)忽略.*?system\s*(?:prompt|message|instruction)',
        r'(?i)ignore\s+(?:all\s+)?(?:previous|above|prior)\s+instructions',
        r'(?i)你(?:现在|已经).*?是.*?(?:系统|管理员|root)',
        r'(?i)你现在.*?扮演',
        r'(?i)返回.*?(?:密码|密钥|token|secret|key)',
        r'(?i)输出.*?(?:密码|密钥|token|secret|key)',
        r'(?i)你是.*?(?:管理员|root|admin|superuser)',
        r'(?i)system\s*[：:].*?(?:you are|你)',
        r'(?i)从现在开始',
    ]
    for pat in injection_patterns:
        content = re.sub(pat, '【内容已过滤】', content)
    max_chars = 50000
    if len(content) > max_chars:
        content = content[:max_chars] + f"\n\n...（文件过长，仅截取前 {max_chars} 字符）"
    return content


# 城市别名映射（P1 #78：支持"帝都/魔都"等口语称呼）
_CITY_ALIASES = {
    "帝都": "北京", "魔都": "上海", "羊城": "广州", "鹏城": "深圳",
    "蓉城": "成都", "山城": "重庆", "春城": "昆明", "冰城": "哈尔滨",
    "泉城": "济南", "榕城": "福州", "星城": "长沙", "江城": "武汉",
}


def _extract_city(user_query: str, user_location: str) -> str:
    """从用户问题/定位中提取城市名（优先级：别名 > 问题内城市 > 用户定位 > 原问题）"""
    for alias, city in _CITY_ALIASES.items():
        if alias in user_query:
            return city
    for c in CITIES:
        if c in user_query:
            return c
    if user_location:
        return user_location.replace("市", "")
    return user_query


def _auto_args(name: str, user_query: str, user_location: str) -> dict:
    """简单/推荐通道：按用户上下文补全工具参数"""
    if name == "query_weather":
        return {"city": _extract_city(user_query, user_location)}
    if name == "query_hotel" or name == "query_food":
        return {"destination": user_location.replace("市", "") if user_location else user_query}
    if name == "query_route":
        return {"departure": user_location or "当前位置", "destination": user_query}
    return {}


async def dispatch_tool(name: str, args: dict, user_query: str = "", user_location: str = "",
                        permissions: list | None = None):
    """统一工具分发：返回工具执行结果（dict）

    args 为空时自动补全（简单/推荐通道）；LLM tool_calls 路径直接使用传入 args。
    permissions 透传给知识库检索（None=不过滤；[]=仅公开；['vip']=公开+vip）。
    """
    if not args:
        args = _auto_args(name, user_query, user_location)
    if name == "query_weather":
        return await fetch_weather_async(args.get("city") or _extract_city(user_query, user_location))
    if name in ("query_hotel", "query_route", "query_food"):
        return await call_sub_agent(name, args, user_query)
    if name == "search_knowledge":
        return await search_knowledge(args.get("query") or user_query, permissions=permissions)
    if name == "web_search":
        return await web_search(args.get("query", ""))
    if name == "search_project_knowledge":
        return await search_project_knowledge(args.get("query") or user_query)
    return {"error": f"未知工具: {name}"}


async def build_file_context(req) -> str | None:
    """上传文件注入：读 Redis → 组装文件片段 + 智能引用要求。

    无文件或文件读取失败返回 None（调用方跳过注入）。
    """
    file_ids = getattr(req, "file_ids", None)
    if not file_ids:
        return None
    r = await get_redis()
    file_parts = []
    for fid in file_ids:
        content = await r.get(f"file:{fid}:content")
        meta_raw = await r.get(f"file:{fid}:meta")
        if not content or not meta_raw:
            continue
        meta = json.loads(meta_raw)
        safe = sanitize_uploaded_content(content.decode() if isinstance(content, bytes) else content)
        fname = meta.get("filename", "未知文件")
        ftype = meta.get("parse_note", "文档")
        flen = meta.get("text_length", len(safe))
        file_parts.append(f"【{fname}】({ftype}, {flen}字)\n```\n{safe[:8000]}\n```")
    if not file_parts:
        return None
    return (
        "用户上传了以下文件，请仔细阅读并智能引用：\n\n"
        + "\n\n".join(file_parts)
        + "\n\n### 引用要求\n"
        "1. 首先告知用户你已读取了文件，指出文件名和类型\n"
        "2. 回答中**直接引用文件中的具体内容**，用「文件中提到…」「根据文档第X页…」等方式\n"
        "3. 如果包含图片OCR文字，区分「文档正文」和「图片OCR识别」\n"
        "4. 对文件内容做结构化总结，列要点，不要简单复述全文\n"
        "5. 多个文件时分别说明各自内容"
    )
