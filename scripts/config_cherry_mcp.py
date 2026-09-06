"""为 Cherry Studio 配置 MCP 工具"""
import sqlite3
import json

DB = r"C:\Users\PC\AppData\Roaming\CherryStudio\Data\agents.db"

# 4 个 MCP 工具配置
MCPS = {
    "sqlite": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-sqlite", "."],
        "description": "SQLite 数据库查询工具"
    },
    "fetch": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-fetch"],
        "description": "网页内容抓取工具"
    },
    "chart": {
        "command": "npx",
        "args": ["-y", "mcp-server-chart"],
        "description": "数据可视化图表生成工具"
    },
    "filesystem": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
        "description": "文件系统与代码执行工具"
    }
}

conn = sqlite3.connect(DB)
c = conn.cursor()

mcps_json = json.dumps(MCPS, ensure_ascii=False, indent=2)
print("MCP 配置:")
print(mcps_json)
print()

# 更新 Cherry Assistant 的 mcps
c.execute("UPDATE agents SET mcps = ? WHERE id = ?", (mcps_json, "cherry-assistant-default"))
print(f"✅ 已写入 Cherry Assistant (cherry-assistant-default)")
print(f"   影响行数: {c.rowcount}")

# 更新 Cherry Claw 的 mcps
c.execute("UPDATE agents SET mcps = ? WHERE id = ?", (mcps_json, "cherry-claw-default"))
print(f"✅ 已写入 Cherry Claw (cherry-claw-default)")
print(f"   影响行数: {c.rowcount}")

conn.commit()

# 验证
c.execute("SELECT id, name, mcps FROM agents")
for row in c.fetchall():
    mcps = row[2]
    if mcps:
        tools = list(json.loads(mcps).keys())
        print(f"\n📌 {row[0]} / {row[1]}: {len(tools)} 个 MCP 工具")
        for t in tools:
            print(f"   - {t}")

conn.close()
