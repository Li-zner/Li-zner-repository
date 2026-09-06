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
