"""思考过程元数据过滤：防止 AI 把系统提示词/内部设定/负面信号泄露给用户

抽取自 app/routes/v2.py（2026-09 重构，行为等价纯移动）。
"""
import re

from ..core.logging import setup_logging

logger = setup_logging()

# ============================================================
# 思考过程元数据过滤：防止 AI 把系统提示词/内部设定/负面信号泄露给用户
# ============================================================
# 强指纹：命中任一即判定该思考段泄露 → 整段不展示
# （系统提示词引用、人设设定、系统限制、工具失败等负面信号）
_REASONING_LEAK_PATTERNS = [
    # ---- 系统提示词 / 指令引用 ----
    "system prompt", "system_prompt", "systemPrompt",
    "根据我的系统提示词", "我的系统提示词", "系统提示词要求", "按照系统提示词",
    "my system prompt", "My system prompt", "my system prompt says",
    "according to my system", "According to my system",
    "the instruction says", "instructions say", "per my instructions",
    "the system prompt says", "system says", "as instructed",
    "my instructions say", "my instructions", "per my system",
    "output language requirement", "always respond in",
    "do not output chinese", "unless the user explicitly asks",
    # ---- 中文指令引用 ----
    "输出语言要求", "请始终使用", "不要输出英文", "不要输出中文",
    "按照指令", "根据指令", "指令要求", "系统设定", "系统限制",
    "系统配置", "根据人设", "按人设", "角色设定", "人设要求",
    # ---- 多 Agent 编排指令复述（模型思考时复述注入指令 → 泄露）----
    "不要提", "不要提及", "禁止提及", "不得提及", "别提",
    "多Agent", "多 Agent", "讨论摘要", "自然口吻", "口吻整合",
    "各领域专家", "专家意见", "专家讨论",
    # ---- 工具调用计划复述（模型思考时写出内部函数名/调用签名 → 泄露）----
    "web_search", "search_knowledge", "search_project_knowledge",
    "fetch_weather_async", "call_sub_agent",
    "query_weather", "query_hotel", "query_route", "query_food",
    # ---- 负面信号：工具失败 / 异常 ----
    "工具调用失败", "调用失败", "工具执行失败", "工具超时", "工具报错",
    "tool call failed", "tool failed", "tool execution failed",
    "tool timeout", "tool error", "call failed",
    "重试失败", "重试仍失败", "请求失败", "请求超时",
    "failed to", "error occurred", "an error occurred",
    # ---- 内部实现细节 ----
    "response_format", "json_object", "llm_semaphore",
    "tools_enabled", "knowledge_base", "max_tokens", "temperature",
    "## 核心原则", "## 可用工具", "## 默认值",
    "防幻觉", "最小工具调用", "路由识别",
    "【输出格式】", "只输出JSON",
    # ---- 注入指令复述（system/user 注入段的标志性短语被思考复述 → 泄露）----
    "请以用户最新的消息为准", "用户上传了以下文件", "【提示：", "引用要求",
    "共享上下文",
    # ---- 约束复述（2026-09-05 实测泄露：模型逐条复述输出约束/文件模板规则）----
    "emoji", "中文（简体）", "约束检查", "特定模板", "文件上传场景",
    "重申身份", "身份设定", "意图识别", "功能定位", "语言风格",
    # ---- 供应商/基础设施指纹（2026-09 qwen 切换：模型身份与端点不得外露）----
    "qwen", "deepseek", "通义", "千问", "百炼",
    "dashscope", "aliyuncs", "compatible-mode",
    "enable_thinking", "reasoning_content", "api_key", "api_base",
]

# 整段抑制：即使正常思考也可能提到工具名，但这些是确凿的系统设定引用
_REASONING_SUPPRESS_PATTERNS = [
    "system prompt", "system_prompt", "systemPrompt",
    "根据我的系统提示词", "我的系统提示词", "系统提示词要求", "按照系统提示词",
    "my system prompt", "according to my system",
    "the instruction says", "instructions say", "per my instructions",
    "as instructed", "my instructions say",
    "output language requirement", "always respond in", "do not output chinese",
    "输出语言要求", "请始终使用", "不要输出英文", "不要输出中文",
    "系统设定", "系统限制", "系统配置", "根据人设", "按人设", "角色设定", "人设要求",
    "不要提", "不要提及", "禁止提及", "不得提及", "别提",
    "多Agent", "多 Agent", "讨论摘要", "自然口吻", "口吻整合",
    "工具调用失败", "调用失败", "工具执行失败", "工具超时", "工具报错",
    "tool call failed", "tool failed", "tool execution failed", "tool timeout",
    "重试失败", "重试仍失败",
    # ---- 注入指令复述（同上，强指纹）----
    "请以用户最新的消息为准", "用户上传了以下文件", "【提示：", "引用要求",
    "共享上下文",
    # ---- 供应商/基础设施指纹（模型身份/端点/配置项，出现即认为思考被污染）----
    "qwen", "deepseek", "通义", "千问", "百炼",
    "dashscope", "aliyuncs", "compatible-mode",
    "enable_thinking", "reasoning_content", "api_key", "api_base",
]

# Key 形状（DeepSeek/百炼/OpenRouter 等均为 sk- 前缀）：思考里出现即视为泄露
# 注：不能用 \b——中文汉字在 Python re 里算 \w，"是sk-xxx" 会因无词边界漏检
_KEY_SHAPE_RE = re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{8,}")

# 预编译正则：流式思考每 Chunk 上百次过滤，避免逐模式 in 扫描（P1 #14）
_REASONING_LEAK_RE = re.compile(
    "|".join(re.escape(p) for p in _REASONING_LEAK_PATTERNS), re.IGNORECASE
)
_REASONING_SUPPRESS_RE = re.compile(
    "|".join(re.escape(p) for p in _REASONING_SUPPRESS_PATTERNS), re.IGNORECASE
)


def reasoning_leaked(text: str) -> bool:
    """检测思考内容是否泄露了系统设定/负面信号/模型身份/Key（整段抑制）"""
    if not text:
        return False
    return _REASONING_SUPPRESS_RE.search(text) is not None or _KEY_SHAPE_RE.search(text) is not None


def sanitize_reasoning(text: str, persona_id: str = "") -> str:
    """过滤思考过程中的敏感内容；命中强泄露指纹返回空串（调用方跳过发送）

    仅对 旅游/法律/综合 模式生效；求职助手（me）不过滤（其内容正常，
    不涉及工具失败/系统设定泄露）。
    策略：
    1. 整段检测：命中系统设定/失败等强指纹 → 整段不展示（返回空串）
    2. 否则逐行剔除敏感碎片，保留正常推理（含重复用户输入，可接受）
    """
    if not text:
        return ""
    # 求职助手不过滤（用户明确要求）
    if persona_id == "me":
        return text
    if reasoning_leaked(text):
        return ""
    lines = text.split("\n")
    kept = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            kept.append(line)
            continue
        if _REASONING_LEAK_RE.search(stripped):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


class ReasoningStreamGuard:
    """流式思考过滤（跨块安全）：强指纹命中即整流抑制，其余逐块行过滤

    ponytail: 跨块检测靠 _OVERLAP 字符尾部重叠拼接，可捕获 ≤(重叠+1) 字符指纹的
    任意两块切分（现有最长指纹约 22 字符）；分成 3 块以上的极端长指纹理论可绕过，
    升级路径是整段缓冲按行送检。
    """
    _OVERLAP = 24

    def __init__(self, persona_id: str = ""):
        self._persona_id = persona_id
        self._carry = ""   # 上一块未参与拼接检测的原始尾部（用于跨块指纹拼接）
        self._dead = False  # 强指纹命中后的整流抑制开关

    def feed(self, chunk: str) -> str:
        """输入一个思考增量，返回可安全展示的文本（可能为空串）"""
        if self._dead or not chunk:
            return ""
        if self._persona_id == "me":
            return chunk  # 求职助手不过滤（与 sanitize_reasoning 豁免一致）
        scan = self._carry + chunk
        if _REASONING_SUPPRESS_RE.search(scan) or _KEY_SHAPE_RE.search(scan):
            self._dead = True
            logger.warning("思考内容命中强泄露指纹（含跨块拼接），后续思考整流抑制")
            return ""
        self._carry = scan[-self._OVERLAP:]
        return sanitize_reasoning(chunk, self._persona_id)
