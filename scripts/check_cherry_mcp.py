"""查看 Cherry Studio 现有 MCP 配置"""
import sqlite3
import json

db_path = r"C:\Users\PC\AppData\Roaming\CherryStudio\Data\agents.db"

conn = sqlite3.connect(db_path)
c = conn.cursor()

# 查看 agents 表的 mcps 列
c.execute("SELECT id, name, mcps FROM agents LIMIT 5")
rows = c.fetchall()
for row in rows:
    print(f"Agent: {row[0]} / {row[1]}")
    mcps = row[2]
    if mcps:
        try:
            data = json.loads(mcps)
            print(f"  MCPs: {json.dumps(data, indent=2, ensure_ascii=False)[:1000]}")
        except:
            print(f"  MCPs (raw): {mcps[:500]}")
    else:
        print(f"  MCPs: (empty)")
    print()

# 查看 sessions 表的 mcps 列
c.execute("SELECT id, agent_id, mcps FROM sessions LIMIT 3")
rows = c.fetchall()
for row in rows:
    print(f"Session: {row[0]} / agent={row[1]}")
    mcps = row[2]
    if mcps:
        print(f"  MCPs: {mcps[:500]}")
    else:
        print(f"  MCPs: (empty)")
    print()

conn.close()
