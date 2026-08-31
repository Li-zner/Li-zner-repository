"""V1 路由（已废弃）- Dify 代理已移除，请使用 V2 接口"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/v1", tags=["v1"])


@router.post("/chat/stream")
async def chat_stream_v1():
    return JSONResponse(
        status_code=410,
        content={"error": "V1 接口已废弃，Dify 代理已移除，请使用 /v2/chat/stream", "code": "DEPRECATED"}
    )