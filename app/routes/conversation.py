"""对话路由：会话画像 CRUD 与单次对话删除。

画像按 conversation_id 隔离；删除对话事务内同步删除消息与会话画像，
仅当用户已无任何对话时再清跨会话全局画像。
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field

from ..middleware.auth import get_current_user
from ..core.db import get_pool
from ..core.redis import get_redis
from ..core.logging import setup_logging
from ..services.conversation_profiles import (
    delete_conversation_profile,
    get_conversation_profile,
    patch_conversation_profile,
)

logger = setup_logging()

router = APIRouter()


class WeatherDayPayload(BaseModel):
    """会话画像里的单日天气快照。"""

    date: str = Field(default="", max_length=16)
    day_weather: str = Field(default="", max_length=32)
    night_weather: str = Field(default="", max_length=32)
    day_temp: str = Field(default="", max_length=16)
    night_temp: str = Field(default="", max_length=16)
    day_wind: str = Field(default="", max_length=32)
    night_wind: str = Field(default="", max_length=32)


class ConversationProfilePatch(BaseModel):
    """画像局部更新协议；未传字段保持不变。"""

    name: Optional[str] = Field(default=None, max_length=64)
    travelers: Optional[str] = Field(default=None, max_length=32)
    origin: Optional[str] = Field(default=None, max_length=64)
    destination: Optional[str] = Field(default=None, max_length=64)
    budget: Optional[str] = Field(default=None, max_length=64)
    days: Optional[str] = Field(default=None, max_length=32)
    notes: Optional[str] = Field(default=None, max_length=1000)
    weather_city: Optional[str] = Field(default=None, max_length=64)
    weather_updated_at: Optional[str] = Field(default=None, max_length=64)
    weather_forecast: Optional[List[WeatherDayPayload]] = Field(
        default=None, max_length=3
    )


@router.get("/api/conversations/{conversation_id}/profile")
async def get_profile(
    conversation_id: str = Path(..., max_length=128),
    current_user: dict = Depends(get_current_user),
):
    """读取当前用户指定会话的画像；不存在时返回空画像。"""
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        return {
            "conversation_id": conversation_id,
            "profile": await get_conversation_profile(
                conn, current_user["username"], conversation_id
            ),
        }


@router.patch("/api/conversations/{conversation_id}/profile")
async def patch_profile(
    payload: ConversationProfilePatch,
    conversation_id: str = Path(..., max_length=128),
    current_user: dict = Depends(get_current_user),
):
    """创建或局部更新会话画像，显式 null 可清空单字段。"""
    updates = payload.model_dump(exclude_unset=True)
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        profile = await patch_conversation_profile(
            conn, current_user["username"], conversation_id, updates
        )
    return {"conversation_id": conversation_id, "profile": profile}


@router.delete("/api/conversations/{conversation_id}/profile")
async def delete_profile(
    conversation_id: str = Path(..., max_length=128),
    current_user: dict = Depends(get_current_user),
):
    """删除指定会话画像，不改动会话消息。"""
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        deleted = await delete_conversation_profile(
            conn, current_user["username"], conversation_id
        )
    return {"message": "画像已删除", "conversation_id": conversation_id,
            "deleted": deleted}


@router.delete("/api/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: str = Path(..., max_length=128),
    current_user: dict = Depends(get_current_user)
):
    """删除指定对话的消息与会话画像，按需清理全局孤儿画像。"""
    username = current_user["username"]
    profile_cleared = False

    try:
        pool = await get_pool()
        async with pool.acquire(timeout=5) as conn:
            # 2026-09-12 修复（外部复核 P1）：删除、剩余检查与画像清理放同一
            # 事务——并发写入时"检查为空→删画像"的窗口会误删仍有对话的画像。
            # 事务内语句使用同一快照并持有写锁，窗口闭合。
            async with conn.transaction():
                result = await conn.execute(
                    "DELETE FROM conversation_memories "
                    "WHERE user_id = $1 AND conversation_id = $2",
                    username, conversation_id
                )
                deleted_count = result.split()[-1]  # "DELETE X" → "X"
                logger.info(f"删除对话消息: user={username}, conv={conversation_id}, deleted={deleted_count}")
                await conn.execute(
                    "DELETE FROM conversation_profiles "
                    "WHERE user_id = $1 AND conversation_id = $2",
                    username, conversation_id,
                )

                # 清除画像（Bug #4 修复）：画像可能由多个对话共同生成，
                # 删单个对话不能连带清空。仅当用户已无任何对话（画像成孤儿）才清理。
                remaining_conv = await conn.fetchval(
                    "SELECT 1 FROM conversation_memories WHERE user_id = $1 LIMIT 1",
                    username
                )
                if not remaining_conv:
                    await conn.execute(
                        "DELETE FROM user_profiles WHERE user_id = $1",
                        username
                    )
                    profile_cleared = True
                    logger.info(f"已清除用户画像（用户已无对话）: user={username}")
                else:
                    logger.info(f"保留用户画像（用户仍有其他对话）: user={username}")
    except Exception as e:
        # 内部错误只留日志，响应给固定文案防信息泄露（P3 修复）
        logger.error(f"删除对话失败: {e}")
        raise HTTPException(status_code=500, detail="删除对话失败，请稍后再试")

    # 3. 清除 Redis 缓存（历史 + 滚动摘要一并清，防删除后同会话 ID 读到旧摘要，P3 修复）
    try:
        r = await get_redis()
        # 键含 user_id（2026-09-09 审查 P0 IDOR 修复）：与 MemoryManager 新键格式对齐
        await r.delete(
            f"conv:{username}:{conversation_id}",
            f"conv_summary:{username}:{conversation_id}",
            f"conv_pending_travel:{username}:{conversation_id}",
        )
        logger.info(f"已清除 Redis 缓存: conv:{username}:{conversation_id}")
    except Exception as e:
        logger.warning(f"Redis 缓存清除失败（不影响主流程）: {e}")

    return {
        "message": "对话已删除",
        "conversation_id": conversation_id,
        "profile_cleared": profile_cleared  # P3 修复：按实际清理结果返回（原先恒 True）
    }
