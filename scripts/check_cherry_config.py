"""检查 Cherry Studio MCP 配置的完整内容和格式"""
import sqlite3
import json

DB = r"C:\Users\PC\AppData\Roaming\CherryStudio\Data\agents.db"
conn = sqlite3.connect(DB)
c = conn.cursor()

# 查看 agents 表全部列
c.execute("PRAGMA table_info(agents)")
cols = c.fetchall()
print("=== agents 表结构 ===")
for col in cols:
    print(f"  {col[1]} ({col[2]})")

# 查看 Cherry Assistant 的完整配置
c.execute("SELECT * FROM agents WHERE id='cherry-assistant-default'")
row = c.fetchone()
if row:
    col_names = [d[1] for d in cols]
    print("\n=== Cherry Assistant 完整数据 ===")
    for i, name in enumerate(col_names):
        val = row[i]
        if val and len(str(val)) > 200:
            print(f"  {name}: {str(val)[:200]}...")
        else:
            print(f"  {name}: {val}")

# 重点检查 mcps 字段
print("\n=== mcps 字段 (JSON 解析) ===")
mcps_raw = row[col_names.index("mcps")] if row else None
if mcps_raw:
    try:
        mcps_data = json.loads(mcps_raw)
        print(json.dumps(mcps_data, indent=2, ensure_ascii=False))
    except json.JSONDecodeError as e:
        print(f"JSON 解析失败: {e}")
        print(f"原始内容: {mcps_raw[:500]}")

# 检查 Cherry Studio 是否有自己的 MCP 缓存
import os
cache_dir = os.path.expanduser(r"~\AppData\Roaming\CherryStudio\Cache")
if os.path.exists(cache_dir):
    print(f"\n=== Cache 目录 ===")
    for f in os.listdir(cache_dir)[:10]:
        fpath = os.path.join(cache_dir, f)
        size = os.path.getsize(fpath)
        print(f"  {f} ({size} bytes)")

conn.close()
