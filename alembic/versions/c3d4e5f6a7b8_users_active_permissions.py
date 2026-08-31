"""users 表补齐：is_active / permissions / phone / tenant_id 兜底

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7

背景（遗留 A8/A10）：
- auth.get_user 一直查询 permissions/phone/tenant_id 等列，但基线迁移未建、仅部分补列，
  需统一补齐，避免"代码查列但表无列"的运行隐患。
- is_active 用于账号禁用（管理员封禁后禁止登录/鉴权）。
"""
from alembic import op

revision: str = 'c3d4e5f6a7b8'
down_revision = 'b2c3d4e5f6a7'


def upgrade() -> None:
    # A8：账号禁用标志（默认启用，存量用户不受影响）
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE")
    # A10：补齐 auth.get_user 依赖的列（幂等 ADD COLUMN IF NOT EXISTS）
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS permissions JSONB DEFAULT '[]'::jsonb")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS phone TEXT")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS tenant_id INT DEFAULT 1")
    # 手机号登录/绑定查询索引（部分索引，仅非空行）
    op.execute("CREATE INDEX IF NOT EXISTS idx_users_phone ON users (phone) WHERE phone IS NOT NULL")


def downgrade() -> None:
    # 仅移除本迁移新增的列；phone/tenant_id 由其它迁移/建表逻辑维护，不回滚
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS is_active")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS permissions")
    op.execute("DROP INDEX IF EXISTS idx_users_phone")
