import hashlib
import random
import time
import uuid
from collections import OrderedDict
from typing import Optional
from ..core.db import get_pool
from ..core.redis import get_redis
from ..core.logging import setup_logging
from ..core.metrics import semantic_cache_hits_total, semantic_cache_misses_total
from ..core.config import CACHE_SIMILARITY_THRESHOLD, CACHE_L0_TTL

logger = setup_logging()


class _LRUCache:
    """进程内 LRU 热缓存（L0），带 TTL + 随机过期（防雪崩）"""

    def __init__(self, capacity: int = 256, default_ttl: int = 600):
        self._cache = OrderedDict()   # key -> (value, expire_ts)
        self._capacity = capacity
        self._default_ttl = default_ttl

    def get(self, key: str):
        item = self._cache.get(key)
        if item is None:
            return None
        value, expire = item
        if time.time() > expire:
            self._cache.pop(key, None)   # 过期即淘汰
            return None
        self._cache.move_to_end(key)
        return value

    def set(self, key: str, value: str, ttl: int = None):
        ttl = ttl if ttl is not None else self._default_ttl
        self._cache[key] = (value, time.time() + ttl)
        self._cache.move_to_end(key)
        if len(self._cache) > self._capacity:
            self._cache.popitem(last=False)

    def __len__(self):
        return len(self._cache)


# 全局热缓存实例（默认 TTL 从 config 读取，P1 去硬编码）
_hot_cache = _LRUCache(capacity=256, default_ttl=CACHE_L0_TTL)

# 释放重建锁的 Lua（仅持有者能删，防误删）
_RELEASE_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


def _hot_ttl() -> int:
    """随机过期时间（300-900s）：错开失效时刻，防缓存雪崩"""
    return random.randint(300, 900)


class SemanticCache:
    # 文本相似度阈值（从 config 读取，默认 0.85）
    SIMILARITY_THRESHOLD = CACHE_SIMILARITY_THRESHOLD

    @staticmethod
    async def get(query: str):
        """
        查询缓存（三级：L0 热缓存 → L1 精确匹配 → L2 语义匹配）
        命中时增加 hit_count，返回 response；未命中记录 miss。
        """
        # L0: 进程内 LRU 热缓存（微秒级）
        l0_result = _hot_cache.get(query)
        if l0_result is not None:
            semantic_cache_hits_total.inc()
            return l0_result

        pool = await get_pool()
        query_hash = hashlib.sha256(query.encode()).hexdigest()

        async with pool.acquire() as conn:
            # L1: 精确匹配（毫秒级）
            row = await conn.fetchrow(
                "SELECT response FROM semantic_cache WHERE query_hash = $1",
                query_hash
            )
            if row:
                await conn.execute(
                    "UPDATE semantic_cache SET hit_count = hit_count + 1 WHERE query_hash = $1",
                    query_hash
                )
                logger.info(f"✅ 精确缓存命中: {query[:30]}...")
                semantic_cache_hits_total.inc()
                # 回填 L0（随机过期防雪崩）
                _hot_cache.set(query, row["response"], ttl=_hot_ttl())
                return row["response"]

            # L2: 相似度匹配（pg_trgm）
            # 用 % 运算符预筛候选（走 GIN 索引），再 similarity 确认，避免全表扫描（P0 #48）
            # 注意：需确保 semantic_cache.query_text 有 pg_trgm GIN 索引（见迁移）
            row = await conn.fetchrow(
                """
                SELECT id, response, similarity(query_text, $1) AS sim
                FROM semantic_cache
                WHERE query_text % $1 AND similarity(query_text, $1) > $2
                ORDER BY sim DESC
                LIMIT 1
                """,
                query,
                SemanticCache.SIMILARITY_THRESHOLD,
            )
            if row:
                await conn.execute(
                    "UPDATE semantic_cache SET query_hash = $1, hit_count = hit_count + 1 WHERE id = $2",
                    query_hash,
                    row["id"],
                )
                logger.info(f"✅ 语义缓存命中 (相似度 {row['sim']:.2f}): {query[:30]}...")
                semantic_cache_hits_total.inc()
                # 回填 L0 + L1
                _hot_cache.set(query, row["response"], ttl=_hot_ttl())
                return row["response"]

            # 未命中
            logger.info(f"❌ 缓存未命中: {query[:30]}...")
            semantic_cache_misses_total.inc()
            return None

    @staticmethod
    async def set(query: str, response: str, ttl: Optional[int] = None):
        """
        写入缓存（同时写入 L0 热缓存 + PG）

        穿透防护：
        - 空/兜底内容不写入（避免把坏数据/兜底回复缓存，导致后续用户永远拿到兜底）
        - 支持自定义 ttl（穿透占位用短 TTL）
        """
        if response is None or not str(response).strip():
            logger.info(f"⛔ 缓存写入跳过（空响应）: {query[:30]}...")
            return
        # 防止把兜底/错误回复写入缓存
        from ..core.config import DEEPSEEK_FALLBACK_MESSAGE
        if str(response).strip() == DEEPSEEK_FALLBACK_MESSAGE:
            logger.info(f"⛔ 缓存写入跳过（兜底回复）: {query[:30]}...")
            return

        # 先写入 L0（随机过期防雪崩）
        _hot_cache.set(query, response, ttl=ttl or _hot_ttl())

        pool = await get_pool()
        query_hash = hashlib.sha256(query.encode()).hexdigest()

        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO semantic_cache (query_hash, query_text, response)
                VALUES ($1, $2, $3)
                ON CONFLICT (query_hash) DO UPDATE
                SET response = $3, query_text = $2
                """,
                query_hash,
                query,
                response,
            )
            logger.info(f"💾 缓存写入: {query[:30]}...")

    @staticmethod
    async def set_empty(query: str, ttl: int = 30):
        """穿透占位：为未命中的 query 写入短暂空值标记（防穿透风暴）

        同一 query 在短时间内重复出现时，通过重建锁 + 该标记避免反复打底层。
        ttl 短（默认 30s），不污染长期缓存。
        """
        pool = await get_pool()
        query_hash = hashlib.sha256(query.encode()).hexdigest()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO semantic_cache (query_hash, query_text, response)
                VALUES ($1, $2, '__EMPTY__')
                ON CONFLICT (query_hash) DO UPDATE SET response = '__EMPTY__', hit_count = 0
                """,
                query_hash,
                query,
            )
        # 同时占位 L0（短 TTL，避免穿透）
        _hot_cache.set(query, "__EMPTY__", ttl=ttl)
        logger.info(f"🛡️ 穿透占位写入: {query[:30]}... (ttl={ttl}s)")

    @staticmethod
    async def is_empty(response) -> bool:
        """判断缓存值是否为穿透占位"""
        return response is not None and str(response) == "__EMPTY__"

    # ===== 防击穿：互斥重建锁 =====
    # 缓存未命中后，同一 query 并发时只允许一个请求调用 LLM 重建，
    # 其余请求等待后重读（可能命中刚写入的缓存）。锁带 TTL 自愈，
    # 避免重建方异常退出导致死锁。
    @staticmethod
    def _lock_key(query: str) -> str:
        return f"cache:rebuild:{hashlib.sha256(query.encode()).hexdigest()}"

    @staticmethod
    async def acquire_rebuild_lock(query: str, ttl: int = 45) -> Optional[str]:
        """尝试获取重建锁；成功返回 token，已有重建进行中返回 None"""
        r = await get_redis()
        token = uuid.uuid4().hex
        ok = await r.set(SemanticCache._lock_key(query), token, nx=True, ex=ttl)
        return token if ok is True else None

    @staticmethod
    async def release_rebuild_lock(query: str, token: str):
        """释放重建锁（仅持有者能释放，防误删）"""
        if not token:
            return
        r = await get_redis()
        await r.eval(_RELEASE_LUA, 1, SemanticCache._lock_key(query), token)

    @staticmethod
    async def renew_rebuild_lock(query: str, token: str, ttl: int = 45) -> bool:
        """续期重建锁（仅持有者可续；长任务重建中周期调用，防锁超时导致击穿，P0 #50）"""
        if not token:
            return False
        r = await get_redis()
        # Lua：仅当仍是本 token 持有才续期，返回 1 表示续期成功
        lua = (
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"
        )
        res = await r.eval(lua, 1, SemanticCache._lock_key(query), token, ttl)
        return res == 1