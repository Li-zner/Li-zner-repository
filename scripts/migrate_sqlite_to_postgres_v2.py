import sqlite3
import asyncpg
import asyncio
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import db_url as make_db_url

# 需要转换日期类型的字段名（可扩展）
DATE_FIELDS = {'created_at', 'updated_at'}

async def migrate():
    # 连接 SQLite
    sqlite_conn = sqlite3.connect('gateway.db')
    sqlite_conn.row_factory = sqlite3.Row
    sqlite_cursor = sqlite_conn.cursor()

    # 连接 PostgreSQL
    dsn = os.getenv("DATABASE_URL") or make_db_url("localhost")
    postgres_conn = await asyncpg.connect(dsn)

    # 获取 SQLite 所有表名
    sqlite_cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = [row[0] for row in sqlite_cursor.fetchall()]
    # 排除系统表
    tables = [t for t in tables if not t.startswith('sqlite_')]

    for table in tables:
        # 检查 SQLite 表是否有数据
        sqlite_cursor.execute(f"SELECT COUNT(*) FROM {table}")
        count = sqlite_cursor.fetchone()[0]
        if count == 0:
            print(f"📭 表 {table} 无数据，跳过")
            continue

        # 获取列信息
        sqlite_cursor.execute(f"PRAGMA table_info({table})")
        columns = sqlite_cursor.fetchall()
        col_names = [col[1] for col in columns]
        col_types = {col[1]: col[2] for col in columns}  # 列名 -> 类型

        columns_str = ', '.join(col_names)
        placeholders = ', '.join(['$' + str(i+1) for i in range(len(col_names))])

        # 读取所有数据
        sqlite_cursor.execute(f"SELECT * FROM {table}")
        rows = sqlite_cursor.fetchall()
        print(f"📤 正在迁移 {len(rows)} 条数据到表 {table}...")

        for row in rows:
            values = list(row)
            # 自动转换日期字段
            for i, col_name in enumerate(col_names):
                if col_name in DATE_FIELDS and values[i] is not None:
                    try:
                        # 尝试将字符串转为 datetime 对象
                        values[i] = datetime.fromisoformat(str(values[i]))
                    except (ValueError, TypeError):
                        # 如果转换失败，保持原值（让 PostgreSQL 自行处理）
                        pass
            try:
                await postgres_conn.execute(
                    f"INSERT INTO {table} ({columns_str}) VALUES ({placeholders}) ON CONFLICT DO NOTHING",
                    *values
                )
            except Exception as e:
                print(f"❌ 插入失败: {e}")
                print(f"   数据: {values}")

    sqlite_conn.close()
    await postgres_conn.close()
    print("✅ 迁移完成！")

if __name__ == '__main__':
    asyncio.run(migrate())