"""
语义缓存预热模块
在服务启动时，将高频查询预写入 pgvector 语义缓存，
减少首次查询的冷启动时间。
"""
import json
import hashlib
import asyncio
from pathlib import Path
from .semantic_cache import SemanticCache
from .logging import setup_logging

logger = setup_logging()

# 高频查询列表（硬编码，来自实际使用统计）
_HOT_QUERIES = [
    # ── 天气 ──
    ("北京今天天气怎么样", None),  # None = 自动运行工具生成
    ("上海天气", None),
    ("广州天气", None),
    ("深圳今天多少度", None),
    ("杭州气温", None),
    # ── 美食 ──
    ("推荐北京好吃的餐厅", None),
    ("上海有什么好吃的", None),
    ("广州美食推荐", None),
    ("成都特色美食", None),
    ("杭州有什么好吃的餐厅", None),
    # ── 路线 ──
    ("从北京到上海怎么去", None),
    ("从广州到深圳怎么走", None),
    ("北京到杭州路线", None),
    # ── 酒店 ──
    ("北京经济型酒店推荐", None),
    ("上海酒店推荐", None),
    ("杭州民宿推荐", None),
    # ── 综合规划 ──
    ("帮我规划一下去杭州的旅游攻略", None),
    ("成都旅游攻略推荐", None),
    # ── 民法典 ──
    ("民法典关于合同纠纷的规定", None),
    ("民法典关于遗产继承", None),
    ("离婚冷静期是多久", None),
]

# 无需调用工具，直接有标准答案的查询
_HOT_ANSWERS = {
    "离婚冷静期是多久": "根据《中华人民共和国民法典》第一千零七十七条规定，自婚姻登记机关收到离婚登记申请之日起三十日内，任何一方不愿意离婚的，可以向婚姻登记机关撤回离婚登记申请。前款规定期限届满后三十日内，双方应当亲自到婚姻登记机关申请发给离婚证；未申请的，视为撤回离婚登记申请。这就是俗称的「离婚冷静期」，为期30天。",
}


async def warmup_semantic_cache():
    """
    启动预热：将高频查询写入语义缓存。
    有标准答案的直接写入；需要工具查询的标记待首次命中后自动填充。
    """
    logger.info("🔥 开始预热语义缓存...")

    # 先写入有标准答案的
    for query, answer in _HOT_ANSWERS.items():
        try:
            await SemanticCache.set(query, answer)
            logger.info(f"  ✅ 预热缓存: {query[:30]}")
        except Exception as e:
            logger.warning(f"  ⚠️ 预热失败: {query[:30]} - {e}")

    # 其余查询标记为"待预热"——首次命中后自动替换为真实结果
    for query, _ in _HOT_QUERIES:
        if query in _HOT_ANSWERS:
            continue
        try:
            # 检查是否已缓存
            cached = await SemanticCache.get(query)
            if cached is None:
                # 写入占位标记，首次访问时会被真实结果覆盖
                placeholder = f"🔮 正在为您查询「{query}」的最新信息，请稍候..."
                await SemanticCache.set(query, placeholder)
                logger.info(f"  📝 标记预热: {query[:30]}")
        except Exception as e:
            logger.warning(f"  ⚠️ 标记失败: {query[:30]} - {e}")

    logger.info(f"✅ 语义缓存预热完成，共处理 {len(_HOT_ANSWERS) + len(_HOT_QUERIES)} 条")