#!/bin/bash
# ============================================================
# 一键部署脚本 - 构建镜像 + 重启容器
# ============================================================

set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

GATEWAY_IMAGE="agent_gateway-gateway:latest"

echo "🔄 开始部署..."

# 1. 重新构建网关镜像（使用 Docker 层缓存加速；依赖未变时秒级完成）
#    注意：不要加 --no-cache，否则每次全量重装 torch/tesseract 会非常慢
echo "🔨 重新构建网关镜像..."
docker build -t "$GATEWAY_IMAGE" .

# 2. 确保 Docker Compose 网络存在
echo "🔌 确保 Docker Compose 网络就绪..."
docker compose up -d --no-recreate postgres redis 2>/dev/null || true

# 3. 重启网关实例（通过 docker compose 统一管理，确保容器命名与 nginx.conf 一致）
echo "🛑 重启网关实例..."
docker compose up -d --force-recreate gateway gateway2 gateway3 gateway4

# 4. 确保 Nginx 负载均衡运行
echo "🚀 确保 Nginx 负载均衡运行..."
docker compose up -d nginx

echo ""
echo "✅ 部署完成！"
echo "📌 直连实例1: http://localhost:10092"
echo "📌 直连实例2: http://localhost:10089"
echo "📌 直连实例3: http://localhost:10093"
echo "📌 直连实例4: http://localhost:10094"
echo "📌 负载均衡: http://localhost:10090"
