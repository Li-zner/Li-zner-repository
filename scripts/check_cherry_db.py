"""检查 Cherry Studio agents.db 的表结构和 MCP 配置"""
import sqlite3
import json

db_path = r"C:\Users\PC\AppData\Roaming\CherryStudio\Data\agents.db"

conn = sqlite3.connect(db_path)
c = conn.cursor()

# 获取所有表
c.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = c.fetchall()
print("Tables:", tables)

# 检查每个表的结构
for table in tables:
    tname = table[0]
    c.execute(f"PRAGMA table_info({tname})")
    cols = c.fetchall()
    print(f"\n{table}:")
    for col in cols:
        print(f"  {col}")

# 查找包含 mcp 的任何内容
for table in tables:
    tname = table[0]
    c.execute(f"SELECT * FROM {tname} LIMIT 3")
    rows = c.fetchall()
    if rows:
        print(f"\n{tname} sample data:")
        for row in rows:
            for val in row:
                if val and isinstance(val, str) and "mcp" in val.lower():
                    print(f"  MCP FOUND: {val[:200]}")

conn.close()
