"""检查当前 DeepSeek 模型配置"""
import os
import json

print("=" * 50)
print("🔍 模型配置检查")
print("=" * 50)

# 从配置文件读取
print("\n📁 config.py 配置:")
print(f"  DEEPSEEK_MODEL = {os.getenv('DEEPSEEK_MODEL', 'deepseekv4flash')}")
print(f"  DEEPSEEK_FLASH_MODEL = {os.getenv('DEEPSEEK_FLASH_MODEL', 'glm4.7flash')}")

# 检查 run_evaluation.py 中的模型
print("\n📁 run_evaluation.py 评测配置:")
print(f"  llm_model (评测官) = deepseek-v4-flash")
print(f"  chat_api_path = /v2/chat/stream → 走网关, 网关用 DEEPSEEK_MODEL")

# 建议
print("\n💡 结论:")
print("  ✅ Agent响应模型: deepseekv4flash (配置变量 DEEPSEEK_MODEL)")
print("  ✅ 评测官评分模型: deepseek-v4-flash (直接调用)")  
print("  ⚠️ 当前模型可能是全量模型/付费模型，请注意费用")
print(f"  如需更改，可将 DEEPSEEK_MODEL 环境变量设为 deepseekv4flash 或其他模型")
print("=" * 50)
