FROM docker.io/library/python:3.11-slim
WORKDIR /app

# 安装系统工具（curl 用于健康检查，tesseract 用于 OCR）
RUN apt-get update && apt-get install -y curl tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-eng && rm -rf /var/lib/apt/lists/*

# 安装依赖（含 OpenTelemetry）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple && \
    pip install --no-cache-dir \
        opentelemetry-distro \
        opentelemetry-instrumentation-fastapi \
        opentelemetry-exporter-otlp-proto-http \
        PyMuPDF \
        pytesseract \
        -i https://pypi.tuna.tsinghua.edu.cn/simple

# 安装 sentence-transformers + CPU-only torch（用于重排序）
RUN pip install --no-cache-dir \
    torch --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir sentence-transformers==3.4.1 \
    -i https://pypi.tuna.tsinghua.edu.cn/simple

COPY app/ ./app/
COPY static/ ./static/
COPY prompts/ ./prompts/
COPY tools/ ./tools/
COPY tests/ ./tests/
# Alembic 迁移 + env 读取（迁移在容器内执行，需这些文件）
COPY alembic/ ./alembic/
COPY alembic.ini .
COPY _env.py .

EXPOSE 10086

# 健康检查：30s间隔，3次失败后重启
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
  CMD curl -f http://localhost:10086/health || exit 1

# docker-compose 会通过 command 覆盖此默认值
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "10086"]