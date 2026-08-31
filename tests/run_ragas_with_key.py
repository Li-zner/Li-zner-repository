"""临时运行器：从 .env 注入 API Key 后执行 ragas_evaluation"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import get

# 密钥全部从 .env 读取，不硬编码
os.environ["DEEPSEEK_API_KEY"] = get("DEEPSEEK_API_KEY")
os.environ["AMAP_API_KEY"] = get("AMAP_API_KEY")

# 执行原脚本
exec(open(os.path.join(os.path.dirname(__file__), "ragas_evaluation.py")).read())
