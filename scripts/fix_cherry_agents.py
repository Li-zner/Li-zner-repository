"""修复 Cherry Studio agents 表数据完整性"""
import sqlite3
import json

DB = r"C:\Users\PC\AppData\Roaming\CherryStudio\Data\agents.db"
conn = sqlite3.connect(DB)
c = conn.cursor()

# 检查所有 agents 行是否完整
c.execute("SELECT * FROM agents")
rows = c.fetchall()
col_names = [d[1] for d in c.description]

print(f"=== agents 表共 {len(rows)} 行 ===")
for row in rows:
    data = dict(zip(col_names, row))
    rid = data.get('id', '?')
    rname = data.get('name', '?')
    rtype = data.get('type', '?')
    rmodel = data.get('model', '?')
    rmcp = bool(data.get('mcps'))
    rallowed = data.get('allowed_tools')
    rcfg = data.get('configuration', '')
    print(f"\n  ID: {rid}")
    print(f"  Name: {rname}")
    print(f"  Type: {rtype}")
    print(f"  Model: {rmodel}")
    print(f"  MCPS: {rmcp}")
    print(f"  Allowed: {rallowed}")
    print(f"  Config: {str(rcfg)[:100] if rcfg else 'None'}")
    
    # 检查必填字段
    if not rtype:
        print("  ⚠️ type 为空!")
    if not rmodel:
        print("  ⚠️ model 为空!")
    if not rname:
        print("  ⚠️ name 为空!")

# 重置 allowed_tools 为 NULL（原始状态）
print("\n\n=== 恢复 allowed_tools 为 NULL ===")
c.execute("UPDATE agents SET allowed_tools = NULL")
c.execute("UPDATE sessions SET allowed_tools = NULL")
conn.commit()
print("✅ 已恢复")

conn.close()

print("\n建议操作:")
print("1. 完全退出 Cherry Studio（包括托盘图标）")
print("2. 重新打开 Cherry Studio")
print("3. 去 设置 → MCP → 添加 Server，手动添加这 4 个工具")
print("   而不是通过数据库配置")
