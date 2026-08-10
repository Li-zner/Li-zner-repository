import json
import asyncio
from typing import List, Dict
from ..core.redis import get_redis
from ..core.db import get_pool
from ..core.logging import setup_logging

logger = setup_logging()

# ============================================================
# 限流队列：控制 PG 写入并发，防止连接池耗尽
# ============================================================
_PG_WRITE_SEMAPHORE = asyncio.Semaphore(5)  # 最多 5 个并发 PG 写入


class MemoryManager:
    """冷热分层记忆管理器（连接池版）"""

    def __init__(self, user_id: str, conv_id: str):
        self.user_id = user_id
        self.conv_id = conv_id

    # ---------- 读路径：L1(Redis) -> L2(PG) 回填 ----------
    async def get_context(self, limit: int = 20, offset: int = 0) -> List[Dict]:
        """
        获取历史消息（支持分页）
        limit: 返回条数
        offset: 偏移量（用于分页加载早期消息）
        """
        redis = await get_redis()
        history_key = f"conv:{self.conv_id}"
        cache_key = f"conv_cache:{self.conv_id}"

        # 首次加载：走 Redis L1 缓存
        if offset == 0:
            raw = await redis.lrange(history_key, -limit, -1)
            if raw:
                return [json.loads(m) for m in raw]

        # L1 缺失：从 PG 分页加载，回填 Redis
        pg_messages = await self._fetch_from_pg(limit, offset)
        if pg_messages and offset == 0:
            # 仅首页回填 Redis 热缓存
            pipe = redis.pipeline()
            for msg in pg_messages:
                pipe.rpush(history_key, json.dumps(msg))
            pipe.expire(history_key, 3600)
            await pipe.execute()
        return pg_messages

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
        history_key = f"conv:{self.conv_id}"
        await redis.rpush(history_key, json.dumps(user_msg))
        await redis.expire(history_key, 3600)

    async def save_messages(self, user_msg: Dict, assistant_msg: Dict):
        """双写：Redis（热） + PG（冷）"""
        redis = await get_redis()
        history_key = f"conv:{self.conv_id}"
        # 先写用户消息，再写助手消息，保持顺序
        await redis.rpush(history_key, json.dumps(user_msg))
        await redis.rpush(history_key, json.dumps(assistant_msg))
        await redis.expire(history_key, 3600)

        import asyncio
        asyncio.create_task(self._save_to_pg(user_msg, assistant_msg))

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
        cities = re.findall(r'([\u4e00-\u9fa5]{2,4}(?:市|州|省|区))', user_content)
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
                    await conn.execute(
                        "DELETE FROM user_profiles WHERE user_id = $1",
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