from pydantic import BaseModel
from typing import Optional, List

class ChatRequest(BaseModel):
    query: str
    user: str = "user-001"
    conversation_id: Optional[str] = None
    file_ids: Optional[List[str]] = None  # 上传的文件ID列表
    user_location: Optional[str] = None  # 用户所在城市，由前端浏览器定位获取
    persona_id: Optional[str] = None  # 人格ID，用于切换角色
    lang: Optional[str] = "zh"  # 界面语言：zh / en，用于控制 LLM 输出语言

# ===== 任务制接口 =====

class CreateTaskRequest(BaseModel):
    session_id: str
    message: str
    user_location: str = ""
    persona_id: str = ""
    file_ids: Optional[List[str]] = None