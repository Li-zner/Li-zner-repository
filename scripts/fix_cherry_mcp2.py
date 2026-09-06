"""修复 Cherry Studio MCP - allowed_tools + 重启后验证"""
import sqlite3
import json

DB = r"C:\Users\PC\AppData\Roaming\CherryStudio\Data\agents.db"
conn = sqlite3.connect(DB)
c = conn.cursor()

# 设置 allowed_tools 为空数组（而非 NULL），避免 "Not a valid Agents array"
empty_list = json.dumps([])
c.execute("UPDATE agents SET allowed_tools = ?", (empty_list,))
print(f"✅ agents.allowed_tools 已设为 [] ({c.rowcount} 条)")

# 同时也更新 sessions
c.execute("UPDATE sessions SET allowed_tools = ?", (empty_list,))
print(f"✅ sessions.allowed_tools 已设为 [] ({c.rowcount} 条)")

conn.commit()

# 验证
c.execute("SELECT id, name, allowed_tools, mcps IS NOT NULL FROM agents")
for row in c.fetchall():
    has_mcp = "有 MCP" if row[3] else "无 MCP"
    print(f"  {row[0]}: allowed={row[2]}, {has_mcp}")

# 检查是否有 agents 数据问题
c.execute("SELECT id, type, name, model FROM agents")
for row in c.fetchall():
    print(f"  Agent: {row[0]} | type={row[1]} | name={row[2]} | model={row[3]}")

conn.close()
print("\n✅ 已修复！请重启 Cherry Studio 后测试")
