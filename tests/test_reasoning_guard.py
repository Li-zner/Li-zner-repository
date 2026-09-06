"""思考内容拦截自检：系统词/模型身份/Key 不外露（2026-09 qwen 切换配套）

覆盖：ReasoningStreamGuard 跨块拼接检测与整流抑制、persona 豁免、
sanitize_error_text 错误文案卫生、stream_llm_throttled 接线。
运行：.venv/Scripts/python.exe tests/test_reasoning_guard.py
"""
import asyncio
import os
import sys
from types import SimpleNamespace

os.environ["APP_ENV"] = "test"
os.environ.setdefault("SESSION_SECRET_KEY", "test-secret")
os.environ.setdefault("JWT_SECRET", "test-jwt")
os.environ.setdefault("ADMIN_PASSWORD", "test-admin")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.reasoning_guard import ReasoningStreamGuard, reasoning_leaked
from app.core.safety_filter import sanitize_error_text

# 1) 正常思考原样通过
g = ReasoningStreamGuard()
assert g.feed("用户想去北京旅游，我先考虑天气因素。") != ""

# 2) 供应商/基础设施指纹：单词块命中 → 抑制，且后续整流抑制（粘性）
g = ReasoningStreamGuard()
assert g.feed("这个问题应该调用百炼的接口") == ""
assert g.feed("后面不管多正常") == ""  # 粘性：命中后不再放行

# 3) 跨块切块：指纹被切成两半，单看每块都不命中，拼接后必须命中
g = ReasoningStreamGuard()
assert g.feed("我在想用户提到dashscop") != ""     # 前半块放行（无完整指纹）
assert g.feed("e.com 的兼容端点怎么用") == ""      # 拼接命中 → 抑制
assert g.feed("之后的正常内容") == ""              # 已整流抑制

# 4) Key 形状（sk- 前缀）出现即抑制
g = ReasoningStreamGuard()
assert g.feed("密钥是sk-000000000000000000000000deadbeef请保密") == ""
assert reasoning_leaked("端点 https://x.api.deepseek.com/chat") is True

# 5) persona "me" 豁免（与 sanitize_reasoning 同语义）
g = ReasoningStreamGuard("me")
assert g.feed("思考里提到 qwen 也原样通过") == "思考里提到 qwen 也原样通过"

# 6) 错误文案卫生：URL（可能带 key 参数）与 sk- Key 剥离
out = sanitize_error_text("Client error '401' for url 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat'")
assert "dashscope" not in out and "http" not in out, out
out = sanitize_error_text("连接失败 key=sk-000000000000000000000000cafebabe")
assert "sk-6a5b" not in out, out
assert sanitize_error_text("") == "服务暂时不可用，请稍后再试"

# 7) stream_llm_throttled 接线：reasoning 增量经过守卫（快速通道裸转发缺口已堵上）
import app.services.llm_streaming as L


async def _fake_stream(api_key, model, messages, **kw):
    for t in ["用户问北京天气，", "我可以调用 dashscope 上的", "query_weather 工具查询"]:
        yield {"type": "reasoning", "text": t}
    yield {"type": "content", "text": "今天晴"}


L.stream_llm = _fake_stream
_ctx = SimpleNamespace(username="u", persona_id="")


async def _collect():
    return [ev async for ev in L.stream_llm_throttled(_ctx, "k", "m", [])]


_out = asyncio.run(_collect())

print("reasoning 事件数:", sum(1 for k, _ in _out if k == "reasoning"))
assert sum(1 for k, _ in _out if k == "reasoning") == 1  # 后两块因 dashscope 指纹被抑制
assert ("answer", "今天晴") in _out

# 8) 真实泄露回归（2026-09-05 用户截图原文，按流式切块喂入）：
#    约束复述/元结构行必须被剔除，草稿等正常思考保留
LEAK_STREAM = [
    "用户要求我“一句话介绍自己”。\n",
    "1.**意图识别**：这是一个非常基础的问候或初始化指令，没有具体的旅行规划需求。\n",
    "2.  **身份设定**：我是“旅游+民法典”，一个全能型智能助手。\n3.**功能定位**：结合旅行规划和民法典咨询。\n",
    "5.**约束检查**：*   使用中文（简体）。*不要使用 emoji。\n",
    "    *如果用户只是上传了文件但没问问题才用特定模板，这里不是文件上传场景。\n",
    "我需要把身份、核心能力以及态度融合在一句话里。\n草稿1：你好，我是旅行规划助手。\n",
    "再检查一下是否包含emoji（无）。符合所有要求。输出结果。\n",
]
g = ReasoningStreamGuard()
shown = "".join(filter(None, (g.feed(t) for t in LEAK_STREAM)))
for banned in ["意图识别", "身份设定", "功能定位", "约束检查", "emoji", "特定模板",
               "中文（简体）", "文件上传场景"]:
    assert banned not in shown, f"泄露未拦截: {banned}"
assert "草稿1" in shown, "正常思考（草稿）被误杀"

# 9) 人格分化：无混合人格，默认人格为 travel
from app.core.persona_manager import get_persona_manager
pm = get_persona_manager()
assert "unified" not in pm._personas, "混合人格 unified 应已删除"
assert pm.current_id == "travel", f"默认人格应为 travel，实际 {pm.current_id}"
assert set(pm._personas) == {"travel", "civil_code", "me"}, set(pm._personas)

print("思考拦截自检全部通过（含真实泄露回归 + 人格分化）")
