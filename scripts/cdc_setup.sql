-- ============================================================
-- CDC（Change Data Capture）初始化 SQL
-- 手工执行参考（应用启动时也会幂等自动创建，此文件用于独立部署）
-- 用法: docker exec -i postgres psql -U agent_user -d agent_gateway < scripts/cdc_setup.sql
-- ============================================================

-- 1) CDC 事件表
CREATE TABLE IF NOT EXISTS cdc_events (
    id BIGSERIAL PRIMARY KEY,
    table_name TEXT NOT NULL,
    op_type TEXT NOT NULL,
    pk_value TEXT NOT NULL DEFAULT '',
    row_before JSONB,
    row_after JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_cdc_events_id ON cdc_events (id);

-- 2) 触发器函数：NEW/OLD 行 -> JSONB -> cdc_events
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
$$ LANGUAGE plpgsql;

-- 3) 三张支付表触发器（主键作为参数传入）
DROP TRIGGER IF EXISTS trg_cdc_payment_orders ON payment_orders;
CREATE TRIGGER trg_cdc_payment_orders
AFTER INSERT OR UPDATE OR DELETE ON payment_orders
FOR EACH ROW EXECUTE FUNCTION cdc_capture('order_no');

DROP TRIGGER IF EXISTS trg_cdc_transaction_logs ON transaction_logs;
CREATE TRIGGER trg_cdc_transaction_logs
AFTER INSERT OR UPDATE OR DELETE ON transaction_logs
FOR EACH ROW EXECUTE FUNCTION cdc_capture('id');

DROP TRIGGER IF EXISTS trg_cdc_user_wallets ON user_wallets;
CREATE TRIGGER trg_cdc_user_wallets
AFTER INSERT OR UPDATE OR DELETE ON user_wallets
FOR EACH ROW EXECUTE FUNCTION cdc_capture('user_id');
