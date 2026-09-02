"""全面检查 Cherry Studio 的 MCP 存储机制"""
import sqlite3
import json

DB = r"C:\Users\PC\AppData\Roaming\CherryStudio\Data\agents.db"
conn = sqlite3.connect(DB)
c = conn.cursor()

# 1. 找到所有包含 mcp 的表或字段
c.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = c.fetchall()
print("=== 所有表 ===")
for t in tables:
    tname = t[0]
    c.execute(f"PRAGMA table_info({tname})")
    cols = c.fetchall()
    mcp_cols = [col for col in cols if 'mcp' in col[1].lower() or 'tool' in col[1].lower()]
    if mcp_cols:
        print(f"\n  {tname}:")
        for col in mcp_cols:
            print(f"    {col[1]} ({col[2]})")
            # 检查数据
            c.execute(f"SELECT {col[1]} FROM {tname} LIMIT 1")
            val = c.fetchone()
            if val and val[0]:
                print(f"    -> {str(val[0])[:300]}")

# 2. 看看有没有存储全局 MCP 的表
c.execute("SELECT * FROM migrations ORDER BY version DESC LIMIT 5")
print("\n=== 最近的迁移 ===")
for row in c.fetchall():
    print(f"  v{row[0]}: {row[1]} ({row[2]})")

# 3. 查看 sessions 表的 mcps
print("\n=== sessions 表的 mcps ===")
c.execute("SELECT id, agent_id, mcps FROM sessions LIMIT 3")
for row in c.fetchall():
    mcps = row[2]
    if mcps:
        print(f"  Session {row[0]} ({row[1]}): {mcps[:200]}")

# 4. 查看配置中是否有 MCP 相关
print("\n=== agents 的 configuration 字段 ===")
c.execute("SELECT id, name, configuration FROM agents")
for row in c.fetchall():
    cfg = row[2]
    if cfg and 'mcp' in cfg.lower():
        print(f"  {row[0]}: {cfg}")
    else:
        print(f"  {row[0]}: (no mcp in config)")

conn.close()

# 5. 检查 Cherry Studio 的其他文件
import os
base = os.path.expanduser(r"~\AppData\Roaming\CherryStudio")
for root, dirs, files in os.walk(os.path.join(base, "Data")):
    for f in files:
        if 'mcp' in f.lower() or 'tool' in f.lower():
            print(f"\n  文件: {os.path.join(root, f)}")
