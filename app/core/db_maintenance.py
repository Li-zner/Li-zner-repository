"""
数据库维护模块
- 对话记忆：全量写入一个月，满月后压缩归档
- 用户画像：按需读写，清理过期画像
- 语义缓存：清理低频缓存
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta

from ..core.db import get_pool
from ..core.logging import setup_logging

logger = setup_logging()

# ============================================================
# 配置
# ============================================================
COMPRESS_AFTER_DAYS = 30       # 对话记忆 30 天后压缩
PROFILE_EXPIRE_DAYS = 90        # 用户画像 90 天未更新则清理
CACHE_CLEAN_DAYS = 60           # 语义缓存 60 天未命中则清理
BATCH_SIZE = 500                # 每批处理条数


async def compress_old_conversations():
    """
    压缩 30 天前的对话记忆：
    1. 按 (user_id, conversation_id) 分组
    2. 将整组对话合并为一条摘要
    3. 删除原始详细记录
    4. 插入压缩后的记录
    """
    pool = await get_pool()
    cutoff = datetime.now() - timedelta(days=COMPRESS_AFTER_DAYS)
    logger.info(f"开始压缩 {cutoff.date()} 之前的对话记忆...")

    async with pool.acquire() as conn:
        # 获取需要压缩的对话列表（分组统计）
        rows = await conn.fetch("""
            SELECT user_id, conversation_id, COUNT(*) as msg_count,
                   MIN(created_at) as first_msg, MAX(created_at) as last_msg
            FROM conversation_memories
            WHERE created_at < $1
            GROUP BY user_id, conversation_id
            ORDER BY MIN(created_at)
        """, cutoff)

        if not rows:
            logger.info("没有需要压缩的对话记忆")
            return

        total_compressed = 0
        for row in rows:
            if row["msg_count"] < 2:
                # 只有一条消息的直接删除
                await conn.execute("""
                    DELETE FROM conversation_memories
                    WHERE user_id=$1 AND conversation_id=$2 AND created_at < $3
                """, row["user_id"], row["conversation_id"], cutoff)
                total_compressed += row["msg_count"]
                continue

            # 获取该对话的所有消息内容
            msg_rows = await conn.fetch("""
                SELECT role, content, created_at
                FROM conversation_memories
                WHERE user_id=$1 AND conversation_id=$2 AND created_at < $3
                ORDER BY created_at ASC
            """, row["user_id"], row["conversation_id"], cutoff)

            # 生成摘要
            msg_count = len(msg_rows)
            first_time = msg_rows[0]["created_at"].strftime("%Y-%m-%d %H:%M")
            last_time = msg_rows[-1]["created_at"].strftime("%Y-%m-%d %H:%M")

            # 提取用户主要关注点
            user_messages = [m["content"][:200] for m in msg_rows if m["role"] == "user"]
            user_summary = "；".join(user_messages[:5])
            if len(user_messages) > 5:
                user_summary += f"……（共{len(user_messages)}条用户消息）"

            compressed_content = json.dumps({
                "type": "compressed",
                "original_count": msg_count,
                "period": f"{first_time} ~ {last_time}",
                "summary": f"用户在该对话中主要关注：{user_summary}",
                "full_text": "\n".join([f"{m['role']}: {m['content']}" for m in msg_rows])
            }, ensure_ascii=False)

            # 原子压缩：DELETE 与 INSERT 必须同事务（P0 #44），中断时回滚防对话数据丢失
            async with conn.transaction():
                # 删除原始详细记录
                await conn.execute("""
                    DELETE FROM conversation_memories
                    WHERE user_id=$1 AND conversation_id=$2 AND created_at < $3
                """, row["user_id"], row["conversation_id"], cutoff)

                # 插入压缩后的摘要记录
                await conn.execute("""
                    INSERT INTO conversation_memories
                    (user_id, conversation_id, role, content, created_at)
                    VALUES ($1, $2, 'system', $3, $4)
                """, row["user_id"], row["conversation_id"], compressed_content, row["last_msg"])

            total_compressed += msg_count

            # 每批暂停一下，避免长时间锁表
            if total_compressed % (BATCH_SIZE * 5) == 0:
                await asyncio.sleep(0.1)

        logger.info(f"对话压缩完成，共压缩 {total_compressed} 条消息，{len(rows)} 个对话")


async def clean_expired_profiles():
    """
    清理过期用户画像：
    - 删除 90 天未更新的画像
    """
    pool = await get_pool()
    cutoff = datetime.now() - timedelta(days=PROFILE_EXPIRE_DAYS)
    async with pool.acquire() as conn:
        result = await conn.execute("""
            DELETE FROM user_profiles
            WHERE updated_at < $1
        """, cutoff)
        deleted = result.split()[-1] if result else "0"
        logger.info(f"清理过期用户画像: {deleted} 条")


async def clean_stale_cache():
    """
    清理低频语义缓存：
    - 删除 60 天未命中且创建超过 7 天的缓存
    """
    pool = await get_pool()
    cutoff = datetime.now() - timedelta(days=CACHE_CLEAN_DAYS)
    async with pool.acquire() as conn:
        result = await conn.execute("""
            DELETE FROM semantic_cache
            WHERE created_at < $1 AND hit_count < 2
        """, cutoff)
        deleted = result.split()[-1] if result else "0"
        logger.info(f"清理低频语义缓存: {deleted} 条")


async def ensure_indexes():
    """确保数据库索引存在"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # conversation_memories 时间索引（用于压缩查询）
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_conv_memories_created
            ON conversation_memories (created_at)
        """)
        # user_profiles 更新时间索引（用于过期清理）
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_profiles_updated
            ON user_profiles (updated_at)
        """)
        # conversation_memories 复合索引（用于分组压缩）
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_conv_memories_compress
            ON conversation_memories (user_id, conversation_id, created_at)
        """)
        # semantic_cache 时间索引
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_semantic_cache_created
            ON semantic_cache (created_at)
        """)
        logger.info("数据库索引已确保")


async def run_maintenance():
    """执行全部维护任务"""
    logger.info("开始数据库维护...")
    try:
        await ensure_indexes()
        await compress_old_conversations()
        await clean_expired_profiles()
        await clean_stale_cache()
        # Bug #3：恢复超时卡死的 processing 支付订单（渠道调用期间进程崩溃/重启后
        # 订单永不复原）。独立 try/except：支付表缺失或异常不影响其他维护任务。
        try:
            from ..payment.service import recover_stale_processing
            await recover_stale_processing(age_seconds=300)
        except Exception as e:
            logger.warning(f"支付订单恢复跳过（不影响其他维护）: {e}")
        logger.info("数据库维护完成")
    except Exception as e:
        logger.error(f"数据库维护失败: {e}", exc_info=True)


async def maintenance_loop(interval_hours: int = 24):
    """定时维护循环"""
    while True:
        await run_maintenance()
        logger.info(f"下次维护在 {interval_hours} 小时后")
        await asyncio.sleep(interval_hours * 3600)
