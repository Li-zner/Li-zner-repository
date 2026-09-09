from pydantic import BaseModel, Field
from typing import Optional, List

class ChatRequest(BaseModel):
    # query 长度约束（2026-09-07 审查 P2）：限流只管频率不管单请求体积，
    # 超大 query 直达 LLM；64KB 足够任何正常对话
    query: str = Field(..., min_length=1, max_length=65536)
    user: str  # 必填：真实用户名由鉴权注入（JWT current_user），此处不再提供虚假默认值
    conversation_id: Optional[str] = None
    # 文件数约束（2026-09-07 审查 P2）：每个 id 2 次 Redis GET，源头限 5
    # （与 core/stream_utils.build_file_context 的兜底上限同值）
    file_ids: Optional[List[str]] = Field(default=None, max_length=5)
    persona_id: Optional[str] = None  # 人格ID，用于切换角色
    lang: Optional[str] = "zh"  # 界面语言：zh / en，用于控制 LLM 输出语言

# ===== 任务制接口 =====

class CreateTaskRequest(BaseModel):
    conversation_id: str  # 与 ChatRequest.conversation_id 对齐，全系统一致
    message: str = Field(..., min_length=1, max_length=65536)  # 同 ChatRequest.query
    persona_id: str = ""
    file_ids: Optional[List[str]] = Field(default=None, max_length=5)
    lang: str = "zh"  # 与 ChatRequest.lang 对齐；任务路径据此切换多语言系统提示词