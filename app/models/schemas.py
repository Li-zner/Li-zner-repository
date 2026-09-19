from pydantic import BaseModel, Field, StringConstraints
from typing import Annotated, Optional, List

# 上传文件 ID 的单项限长（2026-09-14，models 日志 09-11 P2）：file_id 为上传时
# 生成的 uuid4 hex（32 字符），128 上限防畸形长串进 Redis 键
_FileId = Annotated[str, StringConstraints(max_length=128)]


class ChatRequest(BaseModel):
    # query 长度约束（2026-09-07 审查 P2）：限流只管频率不管单请求体积，
    # 超大 query 直达 LLM；64KB 足够任何正常对话
    query: str = Field(..., min_length=1, max_length=65536)
    user: str = Field(..., max_length=64)  # 必填：真实用户名由鉴权注入（JWT current_user），此处不再提供虚假默认值
    # 2026-09-12 清欠：conversation_id 进 Redis 键与日志，限长防滥用
    # （前端 newConversationId 生成 conv_+短随机，128 上限余量充足）
    conversation_id: Optional[str] = Field(default=None, max_length=128)
    # 文件数约束（2026-09-07 审查 P2）：每个 id 2 次 Redis GET，源头限 5
    # （与 core/stream_utils.build_file_context 的兜底上限同值）
    file_ids: Optional[List[_FileId]] = Field(default=None, max_length=5)
    persona_id: Optional[str] = Field(default=None, max_length=64)
    lang: Optional[str] = Field(default="zh", max_length=8)  # 界面语言：zh / en

# ===== 任务制接口 =====

class CreateTaskRequest(BaseModel):
    # 2026-09-12 清欠：限长防滥用（同 ChatRequest.conversation_id）
    conversation_id: str = Field(..., max_length=128)  # 与 ChatRequest.conversation_id 对齐，全系统一致
    message: str = Field(..., min_length=1, max_length=65536)  # 同 ChatRequest.query
    persona_id: str = Field(default="", max_length=64)
    file_ids: Optional[List[_FileId]] = Field(default=None, max_length=5)
    lang: str = Field(default="zh", max_length=8)  # 与 ChatRequest.lang 对齐；任务路径据此切换多语言系统提示词
