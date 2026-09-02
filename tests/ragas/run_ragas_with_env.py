"""
RAGAS 运行器 - 从 .env 文件加载 API Key
"""
import os
import sys

# 从 .env 文件读取密钥（先在项目根目录找，再在 tests/ 找）
env_paths = [
    os.path.join(os.path.dirname(__file__), "..", "..", ".env"),  # ragas/../.. = project root
    os.path.join(os.path.dirname(__file__), "..", ".env"),        # ragas/.. = tests/
]
for env_path in env_paths:
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ[key.strip()] = value.strip()
        print(f"📁 从 {env_path} 加载密钥")
        break

# 执行 RAGAS 评测脚本
script = os.path.join(os.path.dirname(__file__), "ragas_evaluation.py")
with open(script, "r", encoding="utf-8") as f:
    exec(f.read())
