"""
语义缓存预热模块：启动时写入有标准答案的固定问答。

修复（P2）：旧实现会给 20 个无答案的热门查询写"正在为您查询…请稍候..."占位，
并声称"首次访问时会被真实结果覆盖"——该机制不存在（缓存命中不会触发生成），
占位会被原样当答案返回；且写入用 cache_ctx=""，运行时永远按 build_cache_ctx
（人格|位置|画像指纹）查询，这些条目实际永远不可达（纯死写）。现仅预热有
标准答案的条目，并按匿名请求的上下文键写入，保证可命中。
"""
from .semantic_cache import SemanticCache
from .logging import setup_logging

logger = setup_logging()

# 高频查询列表（硬编码，来自实际使用统计）

# 无需调用工具，直接有标准答案的查询
_HOT_ANSWERS = {
    "离婚冷静期是多久": "根据《中华人民共和国民法典》第一千零七十七条规定，自婚姻登记机关收到离婚登记申请之日起三十日内，任何一方不愿意离婚的，可以向婚姻登记机关撤回离婚登记申请。前款规定期限届满后三十日内，双方应当亲自到婚姻登记机关申请发给离婚证；未申请的，视为撤回离婚登记申请。这就是俗称的「离婚冷静期」，为期30天。",
}


async def warmup_semantic_cache():
    """启动预热：只写入有标准答案的固定问答（按匿名请求上下文键，保证可命中）"""
    logger.info("开始预热语义缓存...")
    anon_ctx = SemanticCache.build_cache_ctx("", "")
    for query, answer in _HOT_ANSWERS.items():
        try:
            await SemanticCache.set(query, answer, cache_ctx=anon_ctx)
            logger.info(f"  预热缓存: {query[:30]}")
        except Exception as e:
            logger.warning(f"  预热失败: {query[:30]} - {e}")
    logger.info(f"语义缓存预热完成，共 {len(_HOT_ANSWERS)} 条固定答案")
