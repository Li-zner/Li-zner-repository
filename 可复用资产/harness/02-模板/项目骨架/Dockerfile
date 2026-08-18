# Python 服务镜像骨架
# 注意：保持分层安装以利用层缓存（先装依赖，再拷代码）
FROM python:3.11-slim

WORKDIR /app

# 系统依赖（按项目需要增删）
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 依赖层（requirements 不变则命中缓存，避免每次全量重装）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 代码层
COPY . .

EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -sf http://localhost:8000/health || exit 1

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
