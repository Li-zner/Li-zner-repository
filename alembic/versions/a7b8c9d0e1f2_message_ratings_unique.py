"""message_ratings 增加防重唯一索引（Bug #10）

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1

背景：rate_message 先 SELECT 再 INSERT 且无唯一约束，并发下同一回复
（同用户+会话+消息对）可插入重复评分行。
修复：对 (user_id, 会话, 用户消息, 助手消息) 建唯一索引，COALESCE 把 NULL
视同空串，使「NULL 存量行」与「空串新行」按同一键去重；并发后到者命中唯一
冲突，由代码捕获 UniqueViolationError 后返回已有评分。
历史重复数据先清理（保留最早一条），否则唯一索引无法创建。
"""
from alembic import op

revision: str = 'a7b8c9d0e1f2'
down_revision = 'f6a7b8c9d0e1'


def upgrade() -> None:
    # 清理历史重复行：保留 id 最小的最早一条
    op.execute("""
        DELETE FROM message_ratings a
        USING message_ratings b
        WHERE a.id > b.id
          AND a.user_id = b.user_id
          AND COALESCE(a.session_id, '') = COALESCE(b.session_id, '')
          AND COALESCE(a.user_message, '') = COALESCE(b.user_message, '')
          AND COALESCE(a.assistant_message, '') = COALESCE(b.assistant_message, '')
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_message_ratings_dedup
        ON message_ratings (
            user_id,
            COALESCE(session_id, ''),
            COALESCE(user_message, ''),
            COALESCE(assistant_message, '')
        )
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_message_ratings_dedup")
