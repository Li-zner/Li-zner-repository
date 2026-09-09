import json
import os
import asyncio
from typing import List, Dict
from ..core.redis import get_redis
from ..core.db import get_pool
from ..core.logging import setup_logging
from ..core.config import HISTORY_LIMIT, HISTORY_TTL, HISTORY_SUMMARY_TTL
from ..core.concurrency import spawn
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
        # Redis 键含 user_id（2026-09-09 审查 P0 IDOR 修复）：键只含 conv_id 时，
        # 知道他人会话 ID 即可读其历史（注入 LLM 上下文）、写其热缓存（跨用户提示注入）；
        # PG 回源本就按 user_id 过滤，热缓存同口径对齐（旧键随 TTL 自然过期）
        self._history_key = f"conv:{self.user_id}:{self.conv_id}"
        self._summary_key = f"conv_summary:{self.user_id}:{self.conv_id}"

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
            # 仅首页回填 Redis 热缓存。
            # 原子回填（2026-09-07 审查 P1）：裸 pipeline 逐条 RPUSH 时，同会话并发冷启动
            # 会把同一批消息写两遍（LLM 上下文重复，trim 只裁条数不去重）。
            # Lua 内"列表为空才整批写入"原子完成，败者自动跳过，不重不丢。
            await redis.eval(
                self._BACKFILL_LUA, 1, self._history_key,
                HISTORY_TTL, *[json.dumps(m) for m in pg_messages],
            )
            pg_messages = await self._inject_summary(redis, pg_messages)
        return pg_messages

    # 原子回填脚本：KEYS[1]=历史列表，ARGV[1]=TTL，ARGV[2:]=消息 JSON。
    # 列表已存在（含并发回填/新消息先到）即放弃，杜绝重复回填。
    _BACKFILL_LUA = """
if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
for i = 2, #ARGV do redis.call('RPUSH', KEYS[1], ARGV[i]) end
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]))
return 1
"""

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
                "ORDER BY created_at DESC, id DESC LIMIT $3 OFFSET $4",
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
        except Exception as e:
            logger.debug(f"去重检查读取末条消息失败: {e}")
        # 先写用户消息（除非已存在），再写助手消息，保持顺序
        if last != user_msg:
            await redis.rpush(self._history_key, json.dumps(user_msg))
        await redis.rpush(self._history_key, json.dumps(assistant_msg))
        await redis.expire(self._history_key, HISTORY_TTL)
        await self._trim_and_compress(redis)

        # spawn 持强引用：裸 create_task 的后台任务可被 GC 中途回收（2026-09-07 审查 P2）
        spawn(self._save_to_pg(user_msg, assistant_msg), name=f"save_pg:{self.conv_id}")

    # ---------- 条数限制 + 滚动压缩摘要 ----------
    # 原子裁剪：LLEN/LRANGE/LTRIM 三步合一。拆开执行时并发 append 会挤动下标，
    # 一条消息可能既被 LTRIM 裁掉又没进 overflow 列表（热缓存丢消息，P2 修复）
    _TRIM_LUA = """
local llen = redis.call('LLEN', KEYS[1])
local keep = tonumber(ARGV[1])
if llen <= keep then return nil end
local overflow = redis.call('LRANGE', KEYS[1], 0, llen - keep - 1)
redis.call('LTRIM', KEYS[1], -keep, -1)
return overflow
"""

    async def _trim_and_compress(self, redis):
        """保留最近 HISTORY_LIMIT 条；溢出的旧消息异步压缩为滚动摘要"""
        try:
            overflow = await redis.eval(self._TRIM_LUA, 1, self._history_key, HISTORY_LIMIT)
            if overflow:
                spawn(self._compress_overflow(overflow), name=f"compress:{self.conv_id}")
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
            new_summary = await generate_summary(combined, username=self.user_id)
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
    # （save_user_location 已随"出发地"手动输入功能一并移除；画像城市仍由
    # _update_profile_async 从对话文本自然提取）

    async def _update_profile_async(self, conn, user_content: str, assistant_content: str):
        import re
        new_profile = {}
        # 只匹配以 市/州/省/区 结尾的地名（至少2字），避免把"今天""你好"等词误认为城市
        cities = re.findall(r'([\u4e00-\u9fa5]{2,4}(?:市|州|省|区|自治区))', user_content)
        # 过滤带动词前缀的误匹配（"我从广州"被"州"后缀误抓——广州/苏州/杭州本身含州）
        cities = [c for c in cities if not re.match(r'^(我从|我在|我想|我们|想去|去了|回到|飞到|来到)', c)]
        if cities:
            new_profile["recent_cities"] = cities
        budget_match = re.search(r'(\d+)[-~](\d+)?元', user_content)
        if budget_match:
            new_profile["budget"] = budget_match.group(0)
        # 出行人数（"我们4个人/一家3口/2大1小"）
        people_match = re.search(r'(\d+)\s*个?人|一家\s*(\d+)\s*口|(\d+)大\s*(\d+)小', user_content)
        if people_match:
            new_profile["travelers"] = next(g for g in people_match.groups() if g)
        # 规划天数（"玩3天/5天行程"）
        days_match = re.search(r'(\d+)\s*天', user_content)
        if days_match:
            new_profile["days"] = days_match.group(0)
        # 目的地（"去X旅游/去X玩"——地图「去这里」prompt 场景）
        dest_match = re.search(r'去([一-龥]{2,8}?)(?:旅游|玩|旅行)', user_content)
        if dest_match:
            new_profile["destination"] = dest_match.group(1)
        # 出发地（"我从X出发/我从X到"——地图 prompt 场景；"我"字排除误匹配）
        origin_match = re.search(r'我(?:们)?从([一-龥]{2,8}?)(?:出发|到|去)', user_content)
        if origin_match:
            new_profile["origin"] = origin_match.group(1)
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
                # 跳过过期画像（90天未更新；DB 列为无时区 TIMESTAMP/UTC，本地 now() 会随时区漂移）
                from datetime import datetime, timedelta, timezone
                updated = row["updated_at"]
                utc_naive_now = datetime.now(timezone.utc).replace(tzinfo=None)
                if updated and (utc_naive_now - updated) > timedelta(days=90):
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
                    if profile_data.get("origin"):
                        parts.append(f"出发地: {profile_data['origin']}")
                    if profile_data.get("destination"):
                        parts.append(f"目的地: {profile_data['destination']}")
                    if profile_data.get("travelers"):
                        parts.append(f"人数: {profile_data['travelers']}人")
                    if profile_data.get("days"):
                        parts.append(f"行程: {profile_data['days']}")
                    if parts:
                        return f"【用户画像】{' | '.join(parts)}"
                    return f"【用户画像】{json.dumps(profile_data, ensure_ascii=False)}"
                return f"【用户画像】{json.dumps(profile_data, ensure_ascii=False)}"
            return ""
