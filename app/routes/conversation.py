"""对话路由：删除单次对话（消息 + 用户画像）

从 app/main.py 纯移动而来（2026-08-31 模块化，行为等价，零逻辑改动）。
先删除 conversation_memories，再仅当用户已无任何对话时清除用户画像（孤儿画像才清），
最后清 Redis 缓存（失败不影响主流程）。
"""
from fastapi import APIRouter, Depends, HTTPException

from ..middleware.auth import get_current_user
from ..core.db import get_pool
from ..core.redis import get_redis
from ..core.logging import setup_logging

logger = setup_logging()

router = APIRouter()


@router.delete("/api/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    current_user: dict = Depends(get_current_user)
):
    """删除指定对话的所有消息，并清除该用户的画像"""
    username = current_user["username"]
    profile_cleared = False

    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            # 1. 删除对话消息
            result = await conn.execute(
                "DELETE FROM conversation_memories "
                "WHERE user_id = $1 AND conversation_id = $2",
                username, conversation_id
            )
            deleted_count = result.split()[-1]  # "DELETE X" → "X"
            logger.info(f"删除对话消息: user={username}, conv={conversation_id}, deleted={deleted_count}")

            # 2. 清除画像（Bug #4 修复）：画像可能由多个对话共同生成，
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
        await r.delete(f"conv:{username}:{conversation_id}", f"conv_summary:{username}:{conversation_id}")
        logger.info(f"已清除 Redis 缓存: conv:{username}:{conversation_id}")
    except Exception as e:
        logger.warning(f"Redis 缓存清除失败（不影响主流程）: {e}")

    return {
        "message": "对话已删除",
        "conversation_id": conversation_id,
        "profile_cleared": profile_cleared  # P3 修复：按实际清理结果返回（原先恒 True）
    }
