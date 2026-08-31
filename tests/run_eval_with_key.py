"""
评测运行器 - 从 .env 文件加载 API Key
"""
import os
import sys

# 从 .env 文件读取密钥
env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
if os.path.exists(env_path):
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ[key.strip()] = value.strip()

# 执行原评测脚本
script = os.path.join(os.path.dirname(__file__), "run_evaluation.py")
with open(script, "r", encoding="utf-8") as f:
    exec(f.read())
