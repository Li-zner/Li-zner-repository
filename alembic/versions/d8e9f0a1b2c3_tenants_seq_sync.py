"""tenants 主键序列与种子数据对齐（修复显式插 id 未同步序列导致首个租户创建撞主键）

Revision ID: d8e9f0a1b2c3
Revises: a7b8c9d0e1f2

背景：多租户迁移 a1b2c3d4e5f6 用 `INSERT INTO tenants (id, name) VALUES (1, 'default')`
显式播种 id=1，未同步 tenants_id_seq。首次运行时 nextval 返回 1 与既有 id=1 撞主键，
导致 phone_register / phone_login 自动注册创建租户报 tenants_pkey 冲突
（重试后序列被推进才自愈，是隐性间歇故障；全新库播种同样复现）。
修复：setval 到 max(id)，保证下一次 nextval 不冲突。幂等，可重复执行。
"""
from alembic import op

revision: str = 'd8e9f0a1b2c3'
down_revision = 'a7b8c9d0e1f2'


def upgrade() -> None:
    # 序列对齐到现有最大 id：空表视为 1；GREATEST 防 max=0 时序列归零
    op.execute(
        "SELECT setval('tenants_id_seq', "
        "GREATEST((SELECT COALESCE(MAX(id), 1) FROM tenants), 1))"
    )


def downgrade() -> None:
    # 回滚无操作：序列同步无副作用，反向迁移不改变序列（避免破坏数据一致性）
    pass
