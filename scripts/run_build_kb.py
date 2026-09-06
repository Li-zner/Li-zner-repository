"""知识库重建启动器（从 .env 自动读取环境变量）"""
import os, sys, subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import get, db_url

# 设置所需环境变量（密钥全部来自 .env，不硬编码）
os.environ["SESSION_SECRET_KEY"] = get("SESSION_SECRET_KEY")
os.environ["JWT_SECRET"] = get("JWT_SECRET")
os.environ["DEEPSEEK_API_KEY"] = get("DEEPSEEK_API_KEY")
os.environ["DATABASE_URL"] = db_url("localhost")
os.environ["PGPASSWORD"] = get("POSTGRES_PASSWORD")
# 保留现有环境变量
for k in ["EMBEDDING_API_URL", "EMBEDDING_API_KEY", "EMBEDDING_MODEL"]:
    if k in os.environ:
        del os.environ[k]

# 执行 build 脚本
script = os.path.join(os.path.dirname(__file__), "build_knowledge_base.py")
result = subprocess.run([sys.executable, script], cwd=os.path.dirname(os.path.dirname(__file__)))
sys.exit(result.returncode)
