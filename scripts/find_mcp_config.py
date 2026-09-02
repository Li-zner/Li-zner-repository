"""在 Cherry Studio LevelDB 中查找 MCP 配置"""
import os
import json

# Cherry Studio 使用 LevelDB 存储配置，路径在 Local Storage/leveldb
ldb_path = os.path.expanduser(r"~\AppData\Roaming\CherryStudio\Local Storage\leveldb")

if os.path.exists(ldb_path):
    print(f"LevelDB 目录: {ldb_path}")
    files = os.listdir(ldb_path)
    print(f"文件数: {len(files)}")
    for f in sorted(files)[:20]:
        fpath = os.path.join(ldb_path, f)
        size = os.path.getsize(fpath)
        print(f"  {f} ({size} bytes)")
        # 尝试读取小文件内容
        if size < 10000 and not f.endswith('.log'):
            try:
                with open(fpath, 'rb') as lf:
                    data = lf.read()
                    # 查找可读字符串中的 mcp 相关内容
                    text = data.decode('utf-8', errors='replace')
                    if 'mcp' in text.lower():
                        print(f"    -> 包含 MCP 相关文本")
                        for line in text.split('\n'):
                            if 'mcp' in line.lower():
                                print(f"       {line.strip()[:200]}")
            except:
                pass
else:
    print("LevelDB 目录不存在")
