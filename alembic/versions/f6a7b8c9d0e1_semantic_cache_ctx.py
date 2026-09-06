"""semantic_cache 增加上下文维度列（防跨用户缓存泄漏）

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0

背景（Bug #1）：语义缓存只按 query 建键，而回答会注入人格/位置/画像等个性化
上下文，导致 A 用户的个性化回答被缓存命中给 B 用户。
修复：新增 cache_ctx 列（build_cache_ctx 生成：人格|位置|画像指纹），
读写都按 cache_ctx 过滤；同一 query 在不同上下文下各自缓存。
旧缓存行无 cache_ctx 维度，可能是含个性化信息的毒数据，且与新的复合唯一
约束冲突，一律清空（缓存可重建，无数据损失）。
"""
from alembic import op

revision: str = 'f6a7b8c9d0e1'
down_revision = 'e5f6a7b8c9d0'


def upgrade() -> None:
    # 上下文维度列（缺省空串 = 无个性化上下文的共享条目）
    op.execute("ALTER TABLE semantic_cache ADD COLUMN IF NOT EXISTS cache_ctx TEXT NOT NULL DEFAULT ''")
    # 唯一约束从「仅 query_hash」升级为「query_hash + 上下文」
    op.execute("ALTER TABLE semantic_cache DROP CONSTRAINT IF EXISTS semantic_cache_query_hash_key")
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS semantic_cache_ctx_key
        ON semantic_cache (query_hash, cache_ctx)
    """)
    # 清空旧缓存：旧条目无上下文维度，可能是含个性化信息的毒数据，且与新的
    # 复合唯一约束冲突；缓存可重建，清空最安全
    op.execute("DELETE FROM semantic_cache")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS semantic_cache_ctx_key")
    op.execute("ALTER TABLE semantic_cache DROP COLUMN IF EXISTS cache_ctx")
    op.execute("""
        ALTER TABLE semantic_cache
        ADD CONSTRAINT semantic_cache_query_hash_key UNIQUE (query_hash)
    """)
