"""V2 路由层（薄壳）：仅参数校验 + 调用 services 编排 + 组装响应

2026-09 重构：原 1845 行上帝文件已按「路由只做路由」拆分下沉至 app/services/
（chat_stream_core / chat_fast_paths / chat_react / file_upload / agent_tasks 等）。
行为等价，公开契约不变：router（main.py 引用）与 /v2/* HTTP 端点。
"""
from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from ..middleware.auth import get_current_user
from ..models.schemas import ChatRequest, CreateTaskRequest
from ..core.task_manager import get_task
from ..services.agent_tasks import (
    cancel_agent_task, create_agent_task, ensure_task_access, resume_agent_task, wait_task_result,
    task_user_perms,
)
from ..services.chat_stream_core import chat_generate
from ..services.chat_stream_ctx import ensure_chat_allowed
from ..services.file_upload import get_user_file, handle_upload

router = APIRouter(prefix="/v2", tags=["v2"])

# ---------- 非流式接口响应模型 ----------
class UploadResponse(BaseModel):
    """/upload 响应"""
    file_id: str
    filename: str
    ext: str
    size: int
    uploaded_by: str
    uploaded_at: str
    text_length: int
    parse_note: str
    text_preview: str
    text_content: str


class FileInfoResponse(BaseModel):
    """/files/{file_id} 响应"""
    file_id: str
    filename: str
    ext: str
    size: int
    uploaded_by: str
    uploaded_at: str
    text_length: int
    parse_note: str
    text_content: str


class TaskResultResponse(BaseModel):
    """任务结果/状态响应"""
    status: str
    content: str
    conversation_id: str


# ---------- 文件上传 ----------
@router.post("/upload", response_model=UploadResponse)
async def upload_file(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user)
) -> UploadResponse:
    """上传文件（支持 TXT/PDF/图片/Word 等），返回文件ID与提取的文本"""
    return await handle_upload(file, current_user["username"])


@router.get("/files/{file_id}", response_model=FileInfoResponse)
async def get_file_info(
    file_id: str,
    current_user: dict = Depends(get_current_user)
) -> FileInfoResponse:
    """获取已上传文件的信息和内容"""
    return await get_user_file(file_id, current_user)


# ---------- V2 主力路由 ----------
@router.post("/chat/stream")
async def chat_stream_v2(
    req: ChatRequest,
    current_user: dict = Depends(get_current_user)
) -> StreamingResponse:
    """流式对话（SSE）；限流/日配额/试用额度门禁通过后进入流式编排"""
    today = await ensure_chat_allowed(current_user)
    return StreamingResponse(chat_generate(req, current_user, today), media_type="text/event-stream")


# ============================================================
# 任务制接口（替代流式，支持刷新恢复）
# ============================================================

@router.post("/chat/tasks", status_code=201)
async def create_chat_task(
    req: CreateTaskRequest,
    current_user: dict = Depends(get_current_user)
) -> dict:
    """创建 Agent 生成任务，立即返回 task_id"""
    return await create_agent_task(req, current_user)


@router.get("/chat/tasks/{task_id}/result", response_model=TaskResultResponse)
async def get_task_result(
    task_id: str,
    wait: int = 0,
    current_user: dict = Depends(get_current_user)
) -> TaskResultResponse | JSONResponse:
    """查询/等待任务结果（wait=0 立即返回；wait=1 长轮询最多 60 秒）"""
    task = await get_task(task_id)
    if not task:
        return JSONResponse({"error": "task not found"}, status_code=404)
    ensure_task_access(task, current_user)
    return await wait_task_result(task_id, task, wait)


@router.post("/chat/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: str,
    current_user: dict = Depends(get_current_user)
) -> dict:
    """取消生成任务"""
    task = await get_task(task_id)
    if not task:
        return JSONResponse({"error": "task not found"}, status_code=404)
    ensure_task_access(task, current_user)
    return await cancel_agent_task(task_id, task)


@router.post("/chat/tasks/{task_id}/resume")
async def resume_task(
    task_id: str,
    current_user: dict = Depends(get_current_user)
) -> dict:
    """重新生成（创建新任务，丢弃旧草稿）"""
    task = await get_task(task_id)
    if not task:
        return JSONResponse({"error": "task not found"}, status_code=404)
    ensure_task_access(task, current_user)
    return await resume_agent_task(task_id, task, current_user["username"],
                                   task_user_perms(current_user))
