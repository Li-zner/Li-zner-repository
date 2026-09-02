"""对话支撑原语：输出语言指令 / 人格提示词格式化 / 思考展示开关 / DeepSeek Key 轮询

抽取自 app/routes/v2.py（2026-09 重构，行为等价纯移动）。
"""
import asyncio
import os
from typing import Dict

from ..core.logging import setup_logging

logger = setup_logging()


def lang_instruction(req) -> str:
    """根据界面语言生成 LLM 输出语言指令（追加到 system prompt）

    附加"请勿使用 emoji"以抑制模型原生 emoji 输出（产品要求专业、去表情）。
    """
    lang = getattr(req, 'lang', 'zh') or 'zh'
    if lang == 'en':
        return (
            "\n\n【Output Language Requirement】\n"
            "1. Always respond in **English**.\n"
            "2. Keep proper nouns (names, cities, brand/tech terms) as-is or use conventional translations.\n"
            "3. Keep numbers, amounts and dates unchanged.\n"
            "4. Do not output Chinese unless the user explicitly asks.\n"
            "5. Do not use any emoji."
        )
    return (
        "\n\n【输出语言要求】\n"
        "1. 请始终使用**中文（简体）**回答用户的所有问题。\n"
        "2. 除非用户明确要求，否则不要输出英文。\n"
        "3. 请勿使用任何 emoji 表情符号。"
    )


def safe_format_prompt(prompt: str, **kwargs) -> str:
    """安全格式化人格提示词：缺 {today}/{name} 等占位符时不抛 KeyError（P0 #3）

    占位符缺失时返回原始提示词，避免单个坏配置打崩整个请求。
    """
    try:
        return prompt.format(**kwargs)
    except (KeyError, ValueError):
        logger.warning("人格提示词格式化失败（缺少占位符），使用原始提示词")
        return prompt


def hide_reasoning(persona_id: str, persona) -> bool:
    """是否隐藏思考过程：人格配置 show_reasoning=False 或 求职助手(me) 不展示

    说明：show_reasoning 应作为人格配置布尔字段（P2 #24 完整迁移需改 core/persona_manager），
    当前用 getattr 兼容旧配置；未配置时默认展示思考。
    """
    show = getattr(persona, "show_reasoning", True) if persona else True
    return (not show) or persona_id == "me"


# ---------- DeepSeek API Key 轮询（Round Robin + 连续失败剔除）----------
# 模块加载时缓存 Key 列表，避免每次请求重复 os.getenv（P1 #12 消除 3 次系统调用）
_DEEPSEEK_KEYS = [
    k for k in (
        os.getenv("DEEPSEEK_API_KEY", ""),
        os.getenv("DEEPSEEK_API_KEY_2", ""),
        os.getenv("DEEPSEEK_API_KEY_3", ""),
    ) if k
]
_key_fail_count: Dict[str, int] = {}   # key -> 连续失败次数
_key_round_index = 0                   # 轮询游标
_key_lock = asyncio.Lock()             # 保护上面两个共享状态


async def get_deepseek_key() -> str:
    """带权重的 Round Robin 选择 Key，连续失败 >=3 的 Key 暂被剔除（P1 #13）"""
    global _key_round_index   # 函数内 rebind，需声明 global，否则被当作局部变量（UnboundLocalError）
    async with _key_lock:
        if not _DEEPSEEK_KEYS:
            return ""
        healthy = [k for k in _DEEPSEEK_KEYS if _key_fail_count.get(k, 0) < 3]
        if not healthy:
            healthy = _DEEPSEEK_KEYS   # 全部被剔除则重置，避免永久降级
        key = healthy[_key_round_index % len(healthy)]
        _key_round_index = (_key_round_index + 1) % len(healthy)
    tag = f"...{key[-4:]}" if key else "NONE"
    logger.info(f"Key 轮询: {tag}")
    return key


async def mark_key_result(key: str, ok: bool) -> None:
    """记录 Key 调用结果：成功清零失败计数，失败累计（供健康剔除使用）"""
    async with _key_lock:
        if ok:
            _key_fail_count.pop(key, None)
        else:
            _key_fail_count[key] = _key_fail_count.get(key, 0) + 1