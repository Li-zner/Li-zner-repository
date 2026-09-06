"""
GitHub 试用额度 — 累计提问次数限制（绑定手机号后解除）

设计：
- GitHub 登录用户（phone 为空）置 quota_limited=TRUE，可免费提问 GITHUB_QUESTION_LIMIT 次（默认 20，全局共享，跨所有助手）。
- used_requests 用数据库侧原子自增（防并发丢更新）；累计带 WHERE quota_limited=TRUE，
  使调用方可无条件调用——非受限用户是 no-op，绑定手机解除限制后晚到的累计不污染账号。
"""
from ..core.config import GITHUB_QUESTION_LIMIT
from ..core.db import get_pool
from ..core.redis import get_redis
from ..core.logging import setup_logging

logger = setup_logging()

# auth.get_cached_user 用户信息缓存键前缀（单源定义于此，middleware/routes 统一引用）
USER_INFO_CACHE_PREFIX = "user:info:"


async def invalidate_user_cache(username: str) -> None:
    """失效 auth.get_cached_user 的用户信息缓存（TTL 5 分钟）

    used_requests / quota_limited 变更后必须调用：否则额度判断在缓存窗口内
    读到旧计数，GitHub 试用 20 次的上限在窗口内形同虚设（P1 修复）。
    """
    try:
        r = await get_redis()
        await r.delete(f"{USER_INFO_CACHE_PREFIX}{username}")
    except Exception as e:
        logger.debug(f"用户缓存失效失败（TTL 自愈）: {e}")


def is_quota_exhausted(user: dict) -> bool:
    """受限用户累计提问次数 >= 上限（admin 与非受限用户恒 False）

    admin 账号（账密直登）不设试用限制，无论 quota_limited 状态如何。
    """
    if user.get("role") == "admin":
        return False
    return bool(user.get("quota_limited")) and (user.get("used_requests") or 0) >= GITHUB_QUESTION_LIMIT


def remaining_questions(user: dict) -> int:
    """受限用户剩余可提问次数；非受限用户返回 -1（表示无限制，前端已约定，C6 保留该语义）"""
    if user.get("role") == "admin" or not user.get("quota_limited"):
        return -1
    return max(0, GITHUB_QUESTION_LIMIT - (user.get("used_requests") or 0))


async def inc_used_questions(username: str) -> None:
    """受限用户提问次数 +1（带上限条件原子自增，防并发超限）；失败不影响主流程"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            # 原子：仅当未达上限时才自增，DB 层保证并发下不会超限（P0 #62）
            result = await conn.execute(
                "UPDATE users SET used_requests = used_requests + 1, updated_at = CURRENT_TIMESTAMP "
                "WHERE username = $1 AND quota_limited = TRUE AND role != 'admin' "
                "AND used_requests < $2",
                username, GITHUB_QUESTION_LIMIT,
            )
            # 实际自增才失效缓存：额度判断读的是 auth.get_cached_user 的 5 分钟缓存，
            # 不失效的话窗口内所有请求都拿到旧计数，试用上限可被无限突破（P1 修复）
            if result and result != "UPDATE 0":
                await invalidate_user_cache(username)
    except Exception as e:
        logger.warning(f"GitHub 试用额度累计失败（不影响对话）: {e}")
