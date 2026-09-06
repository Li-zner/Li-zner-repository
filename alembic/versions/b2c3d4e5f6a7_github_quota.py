"""GitHub 试用额度：users 加 quota_limited / used_requests

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-17

设计：
- quota_limited：GitHub 登录且未绑定手机号的用户标记（试用额度状态）
- used_requests：累计终身提问次数（全局共享，所有助手；原子自增，见 app/core/quota.py）

存量库兼容：运行时 main.py 启动时也有幂等 ALTER 兜底；本迁移用于规范环境。
早期按 token 计额的 used_tokens 列已弃用，一并清理。
"""
from alembic import op

revision: str = 'b2c3d4e5f6a7'
down_revision = 'a1b2c3d4e5f6'
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS quota_limited BOOLEAN NOT NULL DEFAULT FALSE")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS used_requests BIGINT NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS used_tokens")


def downgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS used_tokens BIGINT NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS used_requests")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS quota_limited")
