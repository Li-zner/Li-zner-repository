"""message_ratings 表（消息评分，1-5 星）

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8

补齐 main.py 启动 DDL 建的表，使 Alembic 迁移覆盖完整（A19/A23：正式 schema 迁移化）。
"""
from alembic import op

revision: str = 'd4e5f6a7b8c9'
down_revision = 'c3d4e5f6a7b8'


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS message_ratings (
            id BIGSERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            session_id TEXT,
            user_message TEXT,
            assistant_message TEXT,
            rating INT NOT NULL CHECK (rating BETWEEN 1 AND 5),
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_message_ratings_user ON message_ratings (user_id, created_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS message_ratings CASCADE")
