"""CDC 数据库结构：事件表 + 触发器函数 + 三张支付表触发器

DDL 是幂等的（CREATE IF NOT EXISTS / CREATE OR REPLACE），
可在应用启动时执行，也可用 scripts/cdc_setup.sql 手工执行。
"""
import asyncpg

# 事件表 + 索引（单条语句执行）
CDC_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS cdc_events (
    id BIGSERIAL PRIMARY KEY,
    table_name TEXT NOT NULL,
    op_type TEXT NOT NULL,
    pk_value TEXT NOT NULL DEFAULT '',
    row_before JSONB,
    row_after JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

CDC_EVENTS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_cdc_events_id ON cdc_events (id)
"""

# 触发器函数：把 NEW/OLD 行转成 JSONB，写入 cdc_events
# 主键列名由触发器参数（第一个参数）传入，兼容三张表不同的主键
CDC_CAPTURE_FUNCTION = """
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
"""

# 三张支付表：表名 -> 主键列
CDC_TRIGGERS = [
    ("payment_orders", "order_no"),
    ("transaction_logs", "id"),
    ("user_wallets", "user_id"),
]


async def ensure_schema(conn: asyncpg.Connection):
    """CDC schema 校验（A19/A23：DDL 由 Alembic 迁移管理，迁移 e5f6a7b8c9d0）

    不再执行建表/触发器 DDL；仅校验 cdc_events 表存在（缺失 fail loudly 提示跑迁移）。
    注意：CDC 仅支持单列主键表（pk_value 存单列值，见 CDC_CAPTURE_FUNCTION，P0 #3/#21）。
    """
    exists = await conn.fetchval(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = 'cdc_events'"
    )
    if not exists:
        raise RuntimeError("cdc_events 表不存在！请先执行数据库迁移：alembic upgrade head")
