"""cdc_events 改为按月 RANGE 分区（created_at）

Revision ID: b1c2d3e4f5a6
Revises: f0a1b2c3d4e5

背景：cdc_events 是触发器自动写入的追加型事件日志，引入按月分区后过期分区可整体
DROP（保留策略见 worker._maintain_partitions：只 DROP 已处理完的过期分区，#15 的
"只删已归档"不变量不变）。

要点：
- 整个迁移在单事务内原子完成（PG DDL 可回滚）：rename → 建分区表 → 迁数据 →
  校验行数 → 删旧表，任何一步失败整体回滚，不存在 cdc_events 消失的中间窗口；
- 触发器 cdc_capture() 按表名 INSERT，无需改动；
- 分区表主键必须包含分区键：PRIMARY KEY (id, created_at)；id 由同一序列供给，
  全局仍单调递增（worker 断点续传按 id 排序，不受影响）；
- 旧序列先改名再建新 BIGSERIAL，避免序列名冲突；旧表 DROP 时旧序列随之消失；
- 预建 [当前-3 个月 .. 当前+2 个月] 分区 + DEFAULT 分区兜底（时钟漂移/漏建分区时
  不至于插入报错），后续分区由 worker 周期补建。
"""
from alembic import op

revision: str = 'b1c2d3e4f5a6'
down_revision = 'f0a1b2c3d4e5'

# 新表 DDL：与原表列一致，主键扩展为 (id, created_at) 以满足分区表唯一约束要求
_CDC_PARTITIONED_DDL = """
    CREATE TABLE cdc_events (
        id BIGSERIAL,
        table_name TEXT NOT NULL,
        op_type TEXT NOT NULL,
        pk_value TEXT NOT NULL DEFAULT '',
        row_before JSONB,
        row_after JSONB,
        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (id, created_at)
    ) PARTITION BY RANGE (created_at)
"""

# 预建月分区：从 3 个月前到 2 个月后（IF NOT EXISTS 幂等），再补 DEFAULT 兜底分区
_INITIAL_PARTITIONS_DDL = """
    DO $$
    DECLARE m TIMESTAMPTZ;
    BEGIN
        FOR m IN SELECT generate_series(
            date_trunc('month', now()) - interval '3 months',
            date_trunc('month', now()) + interval '2 months',
            interval '1 month')
        LOOP
            EXECUTE format(
                'CREATE TABLE IF NOT EXISTS cdc_events_%s PARTITION OF cdc_events '
                'FOR VALUES FROM (%L) TO (%L)',
                to_char(m, 'YYYY_MM'), m, m + interval '1 month');
        END LOOP;
        EXECUTE 'CREATE TABLE IF NOT EXISTS cdc_events_default PARTITION OF cdc_events DEFAULT';
    END $$;
"""

# 行数一致性校验：不一致直接抛错回滚整个迁移（含数据搬迁）
_ROW_COUNT_GUARD_DDL = """
    DO $$
    BEGIN
        IF (SELECT count(*) FROM cdc_events) IS DISTINCT FROM
           (SELECT count(*) FROM cdc_events_legacy) THEN
            RAISE EXCEPTION 'cdc_events 分区迁移数据校验失败：行数不一致，事务回滚';
        END IF;
    END $$;
"""

# 序列校准：新表 id 接着旧数据最大 id 继续，不重号
_SEQUENCE_CALIBRATE_DDL = """
    SELECT setval('cdc_events_id_seq',
                  GREATEST(COALESCE((SELECT MAX(id) FROM cdc_events), 0) + 1, 1),
                  false)
"""


def upgrade() -> None:
    # 1. 旧表/旧序列改名让位（旧序列跟着旧表改名，避免新 BIGSERIAL 建序列时撞名）
    op.execute("ALTER TABLE cdc_events RENAME TO cdc_events_legacy")
    op.execute("ALTER SEQUENCE cdc_events_id_seq RENAME TO cdc_events_legacy_id_seq")

    # 2. 建分区主表 + 初始分区
    op.execute(_CDC_PARTITIONED_DDL)
    op.execute(_INITIAL_PARTITIONS_DDL)

    # 3. 迁移存量数据（列顺序一致，显式列出防将来列变动踩坑）
    op.execute(
        "INSERT INTO cdc_events (id, table_name, op_type, pk_value, row_before, row_after, created_at) "
        "SELECT id, table_name, op_type, pk_value, row_before, row_after, created_at "
        "FROM cdc_events_legacy"
    )
    op.execute(_SEQUENCE_CALIBRATE_DDL)

    # 4. 校验 + 删旧表（同事务内，校验失败则全部回滚）
    op.execute(_ROW_COUNT_GUARD_DDL)
    op.execute("DROP TABLE cdc_events_legacy")
    op.execute("ANALYZE cdc_events")


def downgrade() -> None:
    # 还原为分区前的扁平表（timestamptz 形态，对应 f0a1b2c3d4e5 之后的状态）
    op.execute("ALTER TABLE cdc_events RENAME TO cdc_events_partitioned")
    op.execute("ALTER SEQUENCE cdc_events_id_seq RENAME TO cdc_events_partitioned_id_seq")

    op.execute("""
        CREATE TABLE cdc_events (
            id BIGSERIAL PRIMARY KEY,
            table_name TEXT NOT NULL,
            op_type TEXT NOT NULL,
            pk_value TEXT NOT NULL DEFAULT '',
            row_before JSONB,
            row_after JSONB,
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_cdc_events_id ON cdc_events (id)")

    op.execute(
        "INSERT INTO cdc_events (id, table_name, op_type, pk_value, row_before, row_after, created_at) "
        "SELECT id, table_name, op_type, pk_value, row_before, row_after, created_at "
        "FROM cdc_events_partitioned"
    )
    op.execute(_SEQUENCE_CALIBRATE_DDL)

    # 连同所有月分区/DEFAULT 分区一起删（CASCADE）
    op.execute("DROP TABLE cdc_events_partitioned CASCADE")
    op.execute("ANALYZE cdc_events")
