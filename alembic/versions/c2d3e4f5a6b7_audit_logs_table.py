"""补建 audit_logs 审计表（P1：core/audit.py 一直在写不存在的表）

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6

背景：core/audit.py 对 audit_logs 做 INSERT/SELECT（登录/注册/手机绑定/检索审计），
但全部历史迁移均未建该表——每次审计写入都以"审计写入失败"告警收场，合规追溯
零记录，admin 查询端点 500。本迁移补建表结构与索引。
"""
from alembic import op

revision: str = 'c2d3e4f5a6b7'
down_revision = 'b1c2d3e4f5a6'


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            id BIGSERIAL PRIMARY KEY,
            user_id TEXT,
            action TEXT NOT NULL,
            detail JSONB,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # 查询路径：admin 按 user_id/action 过滤 + ORDER BY id DESC
    op.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_user ON audit_logs (user_id, id DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_action ON audit_logs (action, id DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit_logs")
