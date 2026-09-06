from pydantic import BaseModel
from typing import Optional, List

class ChatRequest(BaseModel):
    query: str
    user: str  # 必填：真实用户名由鉴权注入（JWT current_user），此处不再提供虚假默认值
    conversation_id: Optional[str] = None
    file_ids: Optional[List[str]] = None  # 上传的文件ID列表
    persona_id: Optional[str] = None  # 人格ID，用于切换角色
    lang: Optional[str] = "zh"  # 界面语言：zh / en，用于控制 LLM 输出语言

# ===== 任务制接口 =====

class CreateTaskRequest(BaseModel):
    conversation_id: str  # 与 ChatRequest.conversation_id 对齐，全系统一致
    message: str
    persona_id: str = ""
    file_ids: Optional[List[str]] = None
    lang: str = "zh"  # 与 ChatRequest.lang 对齐；任务路径据此切换多语言系统提示词