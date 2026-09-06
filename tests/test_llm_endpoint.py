"""llm_endpoint 模型名路由自检：qwen 系走百炼兼容端点，其余走 DeepSeek 官方端点

2026-09 主力模型切换（deepseek → qwen3.7-flash，降级保留 DeepSeek）的配套检查。
运行：.venv/Scripts/python.exe tests/test_llm_endpoint.py
"""
import os
import sys

# config.py 导入即做安全校验，测试环境补齐必需凭据（不落盘）
os.environ["APP_ENV"] = "test"
os.environ.setdefault("SESSION_SECRET_KEY", "test-secret")
os.environ.setdefault("JWT_SECRET", "test-jwt")
os.environ.setdefault("ADMIN_PASSWORD", "test-admin")
os.environ["QWEN_API_KEY"] = "sk-test-qwen"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import llm_endpoint

# 主力模型：qwen 系 → 百炼 OpenAI 兼容端点 + QWEN_API_KEY（忽略传入的 deepseek key）
base, key = llm_endpoint("qwen3.7-flash", "sk-pooled-deepseek")
assert base == "https://dashscope.aliyuncs.com/compatible-mode/v1", base
assert key == "sk-test-qwen", key

# 快照版模型名同样命中 qwen 路由
assert llm_endpoint("qwen3.7-flash-2026-07-15")[0].startswith("https://dashscope")

# 降级模型：deepseek 系 → DeepSeek 官方端点 + 传入的池化 key 原样透传
base, key = llm_endpoint("deepseek-v4-flash", "sk-pooled-deepseek")
assert base == "https://api.deepseek.com", base
assert key == "sk-pooled-deepseek", key

print("llm_endpoint 路由自检通过")
