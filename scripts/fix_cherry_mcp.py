"""修复 Cherry Studio MCP 配置 - 添加 allowed_tools"""
import sqlite3
import json

DB = r"C:\Users\PC\AppData\Roaming\CherryStudio\Data\agents.db"
conn = sqlite3.connect(DB)
c = conn.cursor()

# 1. 查看当前 agents 数据是否完整
c.execute("SELECT id, name, type, instructions FROM agents")
print("=== 当前 agents ===")
for row in c.fetchall():
    inst_len = len(row[3]) if row[3] else 0
    print(f"  {row[0]}: type={row[1]}, type={row[2]}, instructions_len={inst_len}")

# 2. 设置 allowed_tools - 允许所有 MCP 工具
allowed = [
    "sqlite_query", "sqlite_execute", "sqlite_analyze",
    "fetch_get", "fetch_post",
    "chart_create", "chart_render",
    "filesystem_read", "filesystem_write", "filesystem_list",
    "filesystem_search", "filesystem_mkdir"
]
allowed_json = json.dumps(allowed, ensure_ascii=False)

# 或者使用通配符 - 允许所有工具
allowed_all = json.dumps(["*"], ensure_ascii=False)

print(f"\n=== 设置 allowed_tools ===")
print(f"  agents: {allowed_json[:200]}...")

c.execute("UPDATE agents SET allowed_tools = ?", (allowed_all,))
print(f"  更新了 {c.rowcount} 条 records (agents)")

c.execute("UPDATE sessions SET allowed_tools = ?", (allowed_all,))
print(f"  更新了 {c.rowcount} 条 records (sessions)")

conn.commit()

# 3. 验证
c.execute("SELECT id, name, allowed_tools FROM agents")
print(f"\n=== 验证 ===")
for row in c.fetchall():
    print(f"  {row[0]}: allowed_tools={row[2][:100] if row[2] else 'None'}")

conn.close()
print("\n✅ 修复完成，请重启 Cherry Studio")
