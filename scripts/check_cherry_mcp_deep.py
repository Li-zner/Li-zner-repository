"""检查 Cherry Studio 的 MCP 服务器配置和日志"""
import sqlite3
import json
import os

DB = r"C:\Users\PC\AppData\Roaming\CherryStudio\Data\agents.db"
conn = sqlite3.connect(DB)
c = conn.cursor()

# 1. 查看 allowed_tools 在 agents 表中的含义
c.execute("SELECT id, name, allowed_tools, mcps FROM agents")
for row in c.fetchall():
    print(f"\n=== {row[0]} / {row[1]} ===")
    print(f"  allowed_tools: {row[2]}")
    mcps = row[3]
    if mcps:
        data = json.loads(mcps)
        print(f"  mcps keys: {list(data.keys())}")
        # 检查每个 MCP 的格式
        for name, cfg in data.items():
            print(f"    {name}: cmd={cfg.get('command')}, args={cfg.get('args')}")

# 2. 查看是否有 MCP 相关的系统配置
c.execute("SELECT configuration FROM agents WHERE id='cherry-assistant-default'")
cfg_raw = c.fetchone()[0]
if cfg_raw:
    cfg = json.loads(cfg_raw)
    print(f"\n=== configuration ===")
    print(json.dumps(cfg, indent=2, ensure_ascii=False))

# 3. 检查是否有一个 storage 或 settings 表
c.execute("SELECT name FROM sqlite_master WHERE type='table'")
all_tables = [t[0] for t in c.fetchall()]
print(f"\n=== 所有表 ===")
print(all_tables)

conn.close()

# 4. 检查 Cherry Studio 根目录的配置文件
base = r"D:\Agent学习\Cherry Studio"
if os.path.exists(base):
    print(f"\n=== Cherry Studio 安装目录 ===")
    for f in os.listdir(base):
        print(f"  {f}")

# 5. 检查 logs 目录
logs_dir = os.path.expanduser(r"~\AppData\Roaming\CherryStudio\logs")
if os.path.exists(logs_dir):
    print(f"\n=== 日志文件 ===")
    for f in os.listdir(logs_dir)[:10]:
        fpath = os.path.join(logs_dir, f)
        size = os.path.getsize(fpath)
        print(f"  {f} ({size} bytes)")
        # 检查最近的日志是否有 MCP 相关
        if size < 50000:
            try:
                with open(fpath, 'r', encoding='utf-8') as lf:
                    content = lf.read()
                    if 'mcp' in content.lower() or 'error' in content.lower():
                        lines = content.split('\n')
                        for line in lines[-20:]:
                            if 'mcp' in line.lower() or 'error' in line.lower() or 'tool' in line.lower():
                                print(f"    -> {line.strip()[:150]}")
            except:
                pass
