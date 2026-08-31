"""多租户：tenants 表 + users.tenant_id

Revision ID: a1b2c3d4e5f6
Revises: 76a1f09d2ac8
Create Date: 2026-08-16

设计（最小可行版）：
- tenants 表：组织归属层；id=1 为默认租户（存量用户归属）
- users.tenant_id：默认 1（存量用户零迁移）
- 业务表不加 tenant_id：用户隔离已由 username 全局唯一保证，
  租户聚合通过 users JOIN 完成（避免每表加列的大改）
"""
from alembic import op
import sqlalchemy as sa
from typing import Union, Sequence

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '76a1f09d2ac8'
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS tenants (
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    # 默认租户（存量用户归属）
    op.execute("INSERT INTO tenants (id, name) VALUES (1, 'default') ON CONFLICT (id) DO NOTHING")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS tenant_id INT DEFAULT 1")
    op.execute("CREATE INDEX IF NOT EXISTS idx_users_tenant ON users (tenant_id)")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS tenant_id")
    op.execute("DROP TABLE IF EXISTS tenants")
