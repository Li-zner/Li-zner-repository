"""民法典回答依据契约：只允许使用检索到的民法典条文，失败时确定性收口。"""
from __future__ import annotations

import re
from typing import Any

from ..agents.article_normalizer import normalize_article_ref

_ARTICLE_RE = re.compile(
    r"第[一二三四五六七八九十百千零0-9]+条(?:之一|之二|之三|之四)?"
)

_REPLIES = {
    "zh": {
        "no_evidence": (
            "当前知识库中没有检索到能够直接支持回答的《民法典》条文，"
            "我不能基于自身知识继续作答。请补充更具体的事实，或换一种问法后重试。"
        ),
        "ungrounded": (
            "本次回答未能完全对应到已检索的《民法典》条文，已停止输出，"
            "避免提供无依据的法律结论。请重新提问，系统会重新检索。"
        ),
        "service_error": (
            "民法典知识库或生成服务当前不可用，无法基于可靠法条回答，请稍后再试。"
        ),
    },
    "en": {
        "no_evidence": (
            "I could not find Civil Code provisions in the knowledge base that "
            "directly support this question. I cannot answer from general knowledge. "
            "Please add concrete facts or rephrase the question."
        ),
        "ungrounded": (
            "The generated answer did not fully match the retrieved Civil Code "
            "provisions, so it was stopped. Please ask again."
        ),
        "service_error": (
            "The Civil Code knowledge base or generation service is unavailable. "
            "Please try again later."
        ),
    },
}


def grounding_reply(kind: str, lang: str = "zh") -> str:
    """返回契约失败文案；未知类型按服务异常收口，避免透出内部状态。"""
    language = "en" if str(lang).lower().startswith("en") else "zh"
    return _REPLIES[language].get(kind, _REPLIES[language]["service_error"])


def _tool_results(tool_result: Any) -> list[dict]:
    """读取检索结果列表；结构异常统一按无依据处理。"""
    if not isinstance(tool_result, dict):
        return []
    results = tool_result.get("results")
    return results if isinstance(results, list) else []


def civil_tool_result(tool_result: Any) -> dict | None:
    """返回可消费的民法典检索结果；错误、映射或空结果返回 None。"""
    if not isinstance(tool_result, dict) or tool_result.get("error"):
        return None
    if tool_result.get("mapping_hit"):
        return None
    return tool_result if _tool_results(tool_result) else None


def civil_contexts(tool_result: Any) -> list[str]:
    """提取检索依据文本，供法条号溯源校验。"""
    return [
        f"{item.get('heading', '')}\n{item.get('content', '')}"
        for item in _tool_results(tool_result)
        if isinstance(item, dict)
    ]


def _normalized_corpus(contexts: list[str]) -> str:
    """检索依据侧的法条号归一化文本。

    与回答侧同口径（2026-09-19 审查 chat P2-3）：知识库条目可能以阿拉伯数字
    （"第1077条"）或带空格变体书写条号，只归一化回答侧会让正确引用的回答被判
    无依据、整条替换成拒答文案（误杀真答案比放行幻觉更伤用户）。
    """
    return normalize_article_ref("\n".join(contexts or []))


def find_ungrounded_citations(answer: str, contexts: list[str]) -> list[str]:
    """返回回答中出现、但检索依据中不存在的法条号。"""
    normalized = normalize_article_ref(answer or "")
    corpus = _normalized_corpus(contexts)
    return sorted({article for article in _ARTICLE_RE.findall(normalized)
                   if article not in corpus})


def validate_civil_answer(answer: str, tool_result: Any) -> tuple[bool, list[str]]:
    """校验回答至少引用一条检索依据，且不得包含依据外的法条号。"""
    contexts = civil_contexts(tool_result)
    ungrounded = find_ungrounded_citations(answer, contexts)
    normalized = normalize_article_ref(answer or "")
    corpus = _normalized_corpus(contexts)
    cited = set(_ARTICLE_RE.findall(normalized))
    grounded = {article for article in cited if article in corpus}
    return bool(cited) and not ungrounded and cited == grounded, ungrounded
