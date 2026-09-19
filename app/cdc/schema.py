"""CDC 数据库结构：事件表 + 触发器函数 + 三张支付表触发器

DDL 由 Alembic 迁移管理（迁移 e5f6a7b8c9d0 建表/函数/触发器，f0a1b2c3d4e5 改 TIMESTAMPTZ），
应用启动不执行；scripts/cdc_setup.sql 供独立/手工部署参考。本模块仅提供被消费的结构常量与
ensure_schema 校验。注意：CDC 仅支持单列主键表（pk_value 存单列值，见迁移中 cdc_capture 函数）。
"""
import asyncpg

# 三张支付表：表名 -> 主键列
CDC_TRIGGERS = [
    ("payment_orders", "order_no"),
    ("transaction_logs", "id"),
    ("user_wallets", "user_id"),
]


async def ensure_schema(conn: asyncpg.Connection):
    """CDC schema 校验（A19/A23：DDL 由 Alembic 迁移管理）

    不执行建表/触发器 DDL；校验事件表、捕获函数、三张表触发器齐全，
    任一缺失即 fail loudly 提示跑迁移（防“表存在但触发器缺失”导致静默零采集，P1）。
    """
    exists = await conn.fetchval(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = 'cdc_events'"
    )
    if not exists:
        raise RuntimeError("cdc_events 表不存在！请先执行数据库迁移：alembic upgrade head")

    func_exists = await conn.fetchval(
        "SELECT 1 FROM information_schema.routines "
        "WHERE routine_schema = 'public' AND routine_name = 'cdc_capture'"
    )
    if not func_exists:
        raise RuntimeError("cdc_capture 函数不存在！请先执行数据库迁移：alembic upgrade head")

    for table, _pk in CDC_TRIGGERS:
        trig = await conn.fetchval(
            "SELECT 1 FROM information_schema.triggers "
            "WHERE trigger_schema = 'public' AND event_object_table = $1 AND trigger_name = $2",
            table, f"trg_cdc_{table}",
        )
        if not trig:
            raise RuntimeError(f"CDC 触发器 trg_cdc_{table} 不存在！请先执行数据库迁移：alembic upgrade head")

    # 2026-09-14 修复（cdc 日志 09-11 P1）：消费顺序是 (txid,id)，旧 schema 缺
    # txid 列 / 分区结构不对 / 触发器函数未带 txid 采集时，消费会静默漏事件——
    # 启动校验必须覆盖这三处，旧 schema fail loudly 指向迁移。
    txid_col = await conn.fetchval(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'cdc_events' AND column_name = 'txid'"
    )
    if not txid_col:
        raise RuntimeError(
            "cdc_events.txid 列不存在（旧 schema）！消费游标是 (txid,id)，"
            "缺列会漏事件。请执行：alembic upgrade head")
    partstrat = await conn.fetchval(
        "SELECT partstrat FROM pg_partitioned_table WHERE partrelid = 'cdc_events'::regclass"
    )
    # asyncpg 将 PostgreSQL 内部 "char" 类型返回为 bytes，先归一化再比较。
    if isinstance(partstrat, bytes):
        partstrat = partstrat.decode()
    if partstrat != "r":  # 'r' = RANGE（b1c2d3e4f5a6 按月 RANGE 分区）
        raise RuntimeError(
            "cdc_events 不是 RANGE 分区表（旧 schema）！过期分区清理依赖分区结构，"
            "请执行：alembic upgrade head")
    func_src = await conn.fetchval(
        "SELECT pg_get_functiondef('cdc_capture()'::regprocedure)"
    )
    if func_src is None or "txid_current" not in func_src:
        raise RuntimeError(
            "cdc_capture 函数版本过旧（未采集 txid）！消费游标是 (txid,id)，"
            "旧函数产出的事件无法入游标序。请执行：alembic upgrade head")
