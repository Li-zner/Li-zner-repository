"""CDC：事件表 + 触发器函数 + 三表触发器

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9

将 app/cdc/schema.py 的启动 DDL 迁移化（A19/A23：Alembic 唯一入口）。
依赖：payment_orders / transaction_logs / user_wallets 已在基线迁移创建。
"""
from alembic import op

revision: str = 'e5f6a7b8c9d0'
down_revision = 'd4e5f6a7b8c9'


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS cdc_events (
            id BIGSERIAL PRIMARY KEY,
            table_name TEXT NOT NULL,
            op_type TEXT NOT NULL,
            pk_value TEXT NOT NULL DEFAULT '',
            row_before JSONB,
            row_after JSONB,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_cdc_events_id ON cdc_events (id)")

    op.execute("""
        CREATE OR REPLACE FUNCTION cdc_capture() RETURNS TRIGGER AS $$
        DECLARE
            before_json JSONB;
            after_json JSONB;
            pk_val TEXT;
        BEGIN
            pk_val := '';
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                after_json := to_jsonb(NEW);
                IF TG_ARGV[0] IS NOT NULL THEN
                    pk_val := after_json ->> TG_ARGV[0];
                END IF;
            END IF;
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                before_json := to_jsonb(OLD);
                IF TG_OP = 'DELETE' AND TG_ARGV[0] IS NOT NULL THEN
                    pk_val := before_json ->> TG_ARGV[0];
                END IF;
            END IF;
            INSERT INTO cdc_events (table_name, op_type, pk_value, row_before, row_after)
            VALUES (TG_TABLE_NAME, TG_OP, COALESCE(pk_val, ''), before_json, after_json);
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
    """)

    for table, pk in [("payment_orders", "order_no"), ("transaction_logs", "id"), ("user_wallets", "user_id")]:
        op.execute(f"DROP TRIGGER IF EXISTS trg_cdc_{table} ON {table}")
        op.execute(
            f"CREATE TRIGGER trg_cdc_{table} "
            f"AFTER INSERT OR UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION cdc_capture('{pk}')"
        )


def downgrade() -> None:
    for table, _pk in [("payment_orders", "order_no"), ("transaction_logs", "id"), ("user_wallets", "user_id")]:
        op.execute(f"DROP TRIGGER IF EXISTS trg_cdc_{table} ON {table}")
    op.execute("DROP FUNCTION IF EXISTS cdc_capture()")
    op.execute("DROP TABLE IF EXISTS cdc_events CASCADE")
