import json
import os
import asyncio
from typing import List, Dict
from ..core.redis import get_redis
from ..core.db import get_pool
from ..core.logging import setup_logging
from ..core.config import HISTORY_LIMIT, HISTORY_TTL, HISTORY_SUMMARY_TTL
from ..agents.memory import generate_summary

logger = setup_logging()

# ============================================================
# 限流队列：控制 PG 写入并发，防止连接池耗尽
# 连接池 max=50，并发写放大到 12（原 5 太低，拖慢落库）
# ============================================================
# 最多 12 个并发 PG 写入；与连接池上限联动（P1 #42：防连接池耗尽）
_PG_WRITE_SEMAPHORE = asyncio.Semaphore(max(1, min(12, int(os.getenv("DB_POOL_MAX_SIZE", "50")) // 4)))


class MemoryManager:
    """冷热分层记忆管理器（连接池版）"""

    def __init__(self, user_id: str, conv_id: str):
        self.user_id = user_id
        self.conv_id = conv_id
        self._history_key = f"conv:{self.conv_id}"
        self._summary_key = f"conv_summary:{self.conv_id}"

    # ---------- 读路径：L1(Redis) -> L2(PG) 回填 ----------
    async def get_context(self, limit: int = HISTORY_LIMIT, offset: int = 0) -> List[Dict]:
        """
        获取历史消息（支持分页）
        limit: 返回条数
        offset: 偏移量（用于分页加载早期消息）
        首页（offset=0）会注入滚动压缩摘要【历史摘要】，帮助 LLM 理解更早的上下文。
        """
        redis = await get_redis()

        # 首次加载：走 Redis L1 缓存
        if offset == 0:
            raw = await redis.lrange(self._history_key, -limit, -1)
            if raw:
                msgs = [json.loads(m) for m in raw]
                return await self._inject_summary(redis, msgs)

        # L1 缺失：从 PG 分页加载，回填 Redis
        pg_messages = await self._fetch_from_pg(limit, offset)
        if pg_messages and offset == 0:
            # 仅首页回填 Redis 热缓存
            pipe = redis.pipeline()
            for msg in pg_messages:
                pipe.rpush(self._history_key, json.dumps(msg))
            pipe.expire(self._history_key, HISTORY_TTL)
            await pipe.execute()
            pg_messages = await self._inject_summary(redis, pg_messages)
        return pg_messages

    async def _inject_summary(self, redis, msgs: List[Dict]) -> List[Dict]:
        """把滚动压缩摘要注入为第一条（system），供 LLM 理解更早上下文"""
        try:
            summary = await redis.get(self._summary_key)
            if summary:
                summary = summary.decode() if isinstance(summary, bytes) else summary
                if summary and summary.strip():
                    return [{"role": "system", "content": f"【历史摘要】{summary}"}] + msgs
        except Exception as e:
            logger.warning(f"读取对话摘要失败: {e}")
        return msgs

    async def _fetch_from_pg(self, limit: int, offset: int = 0) -> List[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT role, content FROM conversation_memories "
                "WHERE user_id = $1 AND conversation_id = $2 "
                "ORDER BY created_at DESC LIMIT $3 OFFSET $4",
                self.user_id, self.conv_id, limit, offset
            )
            return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    # ---------- 写路径：同步写 Redis，异步写 PG ----------
    async def save_user_message(self, user_msg: Dict):
        """仅保存用户消息（用于流式开始时立即保存，防止刷新丢失）"""
        redis = await get_redis()
        await redis.rpush(self._history_key, json.dumps(user_msg))
        await redis.expire(self._history_key, HISTORY_TTL)
        await self._trim_and_compress(redis)

    async def save_messages(self, user_msg: Dict, assistant_msg: Dict):
        """双写：Redis（热，按条数保留） + PG（冷，全量落库）  去重用户消息"""
        redis = await get_redis()
        # 去重：流式开始时可能已 save_user_message；若末条已是同一用户消息则跳过，
        # 避免完整对话历史里用户消息重复出现、污染 LLM 上下文（P1 修复）。
        last = None
        try:
            last_raw = await redis.lindex(self._history_key, -1)
            if last_raw is not None:
                last = json.loads(last_raw)
        except Exception:
            pass
        # 先写用户消息（除非已存在），再写助手消息，保持顺序
        if last != user_msg:
            await redis.rpush(self._history_key, json.dumps(user_msg))
        await redis.rpush(self._history_key, json.dumps(assistant_msg))
        await redis.expire(self._history_key, HISTORY_TTL)
        await self._trim_and_compress(redis)

        asyncio.create_task(self._save_to_pg(user_msg, assistant_msg))

    # ---------- 条数限制 + 滚动压缩摘要 ----------
    async def _trim_and_compress(self, redis):
        """保留最近 HISTORY_LIMIT 条；溢出的旧消息异步压缩为滚动摘要"""
        try:
            llen = await redis.llen(self._history_key)
            if llen <= HISTORY_LIMIT:
                return
            overflow = await redis.lrange(self._history_key, 0, llen - HISTORY_LIMIT - 1)
            await redis.ltrim(self._history_key, -HISTORY_LIMIT, -1)
            asyncio.create_task(self._compress_overflow(overflow))
        except Exception as e:
            logger.warning(f"对话历史条数裁剪失败: {e}")

    async def _compress_overflow(self, overflow_msgs):
        """滚动摘要：旧摘要 + 本次溢出 → LLM 重新生成摘要（失败不阻塞主流程）"""
        try:
            redis = await get_redis()
            old = await redis.get(self._summary_key)
            old_text = old.decode() if isinstance(old, bytes) else (old or "")
            combined = []
            if old_text and old_text.strip():
                combined.append({"role": "system", "content": f"此前摘要：{old_text}"})
            combined.extend(json.loads(m) for m in overflow_msgs)
            new_summary = await generate_summary(combined)
            if new_summary and new_summary.strip():
                await redis.set(self._summary_key, new_summary, ex=HISTORY_SUMMARY_TTL)
        except Exception as e:
            logger.warning(f"生成滚动摘要失败（不影响主流程）: {e}")

    async def _save_to_pg(self, user_msg: Dict, assistant_msg: Dict):
        """异步写入 PostgreSQL（带限流与错误处理）"""
        async with _PG_WRITE_SEMAPHORE:  # 限流：最多5个并发写入
            pool = await get_pool()
            try:
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        await conn.execute(
                            "INSERT INTO conversation_memories "
                            "(user_id, conversation_id, role, content) VALUES ($1, $2, $3, $4)",
                            self.user_id, self.conv_id, "user", user_msg["content"]
                        )
                        await conn.execute(
                            "INSERT INTO conversation_memories "
                            "(user_id, conversation_id, role, content) VALUES ($1, $2, $3, $4)",
                            self.user_id, self.conv_id, "assistant", assistant_msg["content"]
                        )
                    # 画像更新必须在事务之外执行（P0 #41）：画像失败不会回滚已提交的对话记忆
                    await self._update_profile_async(conn, user_msg["content"], assistant_msg["content"])
            except Exception as e:
                logger.warning(f"PG 写入失败（不影响主流程）: {e}")

    # ---------- L3：用户画像更新 ----------
    async def save_user_location(self, city: str):
        """保存用户定位到画像（异步，不阻塞）"""
        import asyncio
        # 验证城市名称合法性：仅含中文、字母、空格，长度2-20
        import re
        if not city or not re.match(r'^[\u4e00-\u9fa5a-zA-Z\s]{2,20}$', city.strip()):
            logger.warning(f"保存定位跳过：无效的城市名称 '{city}'")
            return
        city = city.strip()
        asyncio.create_task(self._save_location_to_pg(city))

    async def _save_location_to_pg(self, city: str):
        async with _PG_WRITE_SEMAPHORE:
            try:
                pool = await get_pool()
                async with pool.acquire() as conn:
                    new_profile = {"recent_cities": [city], "last_location": city}
                    await conn.execute(
                        "INSERT INTO user_profiles (user_id, profile) VALUES ($1, $2) "
                        "ON CONFLICT (user_id) DO UPDATE "
                        "SET profile = user_profiles.profile || $2, updated_at = NOW()",
                        self.user_id, json.dumps(new_profile)
                    )
            except Exception as e:
                logger.warning(f"保存定位到画像失败: {e}")

    async def _update_profile_async(self, conn, user_content: str, assistant_content: str):
        import re
        new_profile = {}
        # 只匹配以 市/州/省/区 结尾的地名（至少2字），避免把"今天""你好"等词误认为城市
        cities = re.findall(r'([\u4e00-\u9fa5]{2,4}(?:市|州|省|区|自治区))', user_content)
        if cities:
            new_profile["recent_cities"] = cities
        budget_match = re.search(r'(\d+)[-~](\d+)?元', user_content)
        if budget_match:
            new_profile["budget"] = budget_match.group(0)
        if new_profile:
            await conn.execute(
                "INSERT INTO user_profiles (user_id, profile) VALUES ($1, $2) "
                "ON CONFLICT (user_id) DO UPDATE "
                "SET profile = user_profiles.profile || $2, updated_at = NOW()",
                self.user_id, json.dumps(new_profile)
            )

    # ---------- 获取画像（按需读取）----------
    async def get_profile(self) -> str:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT profile, updated_at FROM user_profiles WHERE user_id = $1",
                self.user_id
            )
            if row and row["profile"]:
                # 跳过过期画像（90天未更新）
                from datetime import datetime, timedelta
                updated = row["updated_at"]
                if updated and (datetime.now() - updated) > timedelta(days=90):
                    # 原子条件删除（P1 #44）：带过期条件，避免"读-删"两步竞态与重复清理
                    await conn.execute(
                        "DELETE FROM user_profiles WHERE user_id = $1 "
                        "AND updated_at < NOW() - INTERVAL '90 days'",
                        self.user_id
                    )
                    return ""
                profile_data = row["profile"]
                # 精简画像：只保留最近的3个城市
                if isinstance(profile_data, dict):
                    if "recent_cities" in profile_data:
                        profile_data["recent_cities"] = profile_data["recent_cities"][-3:]
                    # 友好格式化
                    parts = []
                    if profile_data.get("recent_cities"):
                        parts.append(f"最近城市: {', '.join(profile_data['recent_cities'])}")
                    if profile_data.get("budget"):
                        parts.append(f"预算: {profile_data['budget']}")
                    if profile_data.get("last_location"):
                        parts.append(f"定位: {profile_data['last_location']}")
                    if parts:
                        return f"【用户画像】{' | '.join(parts)}"
                    return f"【用户画像】{json.dumps(profile_data, ensure_ascii=False)}"
                return f"【用户画像】{json.dumps(profile_data, ensure_ascii=False)}"
            return ""