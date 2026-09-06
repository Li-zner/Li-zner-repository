"""cdc_events.created_at: TIMESTAMP -> TIMESTAMPTZ（UTC 感知）

Revision ID: f0a1b2c3d4e5
Revises: d8e9f0a1b2c3

把 created_at 从无时区 TIMESTAMP 改为 TIMESTAMPTZ，让 worker 滞后指标与 ts 字段不再
隐式依赖“DB 会话时区=UTC”；存量 naive 值按会话时区 round-trip 转换（UTC 下无偏差）。
"""
from alembic import op

revision: str = 'f0a1b2c3d4e5'
down_revision = 'd8e9f0a1b2c3'


def upgrade() -> None:
    op.execute(
        "ALTER TABLE cdc_events "
        "ALTER COLUMN created_at TYPE TIMESTAMPTZ USING created_at::timestamptz"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE cdc_events "
        "ALTER COLUMN created_at TYPE TIMESTAMP"
    )
