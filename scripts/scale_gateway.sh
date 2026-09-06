#!/bin/bash
# ============================================================
# scale_gateway.sh — 网关实例弹性伸缩助手
#
# 当前 docker-compose 固定 4 实例（gateway~gateway4）。
# 本脚本实现"运行时加/减实例"：
#   - 加实例: 从现有镜像起一个新容器，加入 docker_default 网络，
#             更新 nginx upstream，reload
#   - 减实例: 从 nginx 摘除，停止容器
#
# 用法:
#   bash scale_gateway.sh add <port>      # 例如: bash scale_gateway.sh add 10095
#   bash scale_gateway.sh remove <port>   #      bash scale_gateway.sh remove 10095
#   bash scale_gateway.sh status          # 查看当前实例
# ============================================================

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

IMAGE="agent_gateway-gateway:latest"
NETWORK="docker_default"
ACTION="${1:-status}"
PORT="${2:-}"

next_name() {
    # 找现有 agent_gatewayN 的最大编号，返回下一个
    local max=0
    for c in $(docker ps --format '{{.Names}}' | grep -E '^agent_gateway[0-9]*$'); do
        local n="${c#agent_gateway}"
        [ -z "$n" ] && n=1
        if [ "$n" -gt "$max" ]; then max="$n"; fi
    done
    echo $((max + 1))
}

case "$ACTION" in
    add)
        [ -z "$PORT" ] && { echo "用法: bash scale_gateway.sh add <port>" >&2; exit 1; }
        IDX="$(next_name)"
        NAME="agent_gateway${IDX}"
        echo "==> 新增实例 $NAME (端口 $PORT)"
        docker run -d --name "$NAME" \
            --network "$NETWORK" \
            --env-file '//wsl.localhost/Ubuntu/etc/agent_gateway/.env' \
            -e REDIS_URL=redis://redis:6379/0 \
            -p "$PORT:10086" \
            --restart unless-stopped \
            "$IMAGE" \
            python -m uvicorn app.main:app --host 0.0.0.0 --port 10086
        # 加入 nginx upstream 并 reload
        sed -i "s|    server agent_gateway4:10086 max_fails=3 fail_timeout=30s;|    server agent_gateway4:10086 max_fails=3 fail_timeout=30s;\n    server $NAME:10086 max_fails=3 fail_timeout=30s;|" nginx.conf
        docker exec nginx_lb nginx -s reload || true
        echo "==> 已加入 nginx 负载均衡（请确认 nginx.conf 挂载生效）"
        ;;
    remove)
        [ -z "$PORT" ] && { echo "用法: bash scale_gateway.sh remove <port>" >&2; exit 1; }
        # 找到该端口对应的容器名
        NAME=$(docker ps --format '{{.Names}} {{.Ports}}' | grep ":$PORT->" | awk '{print $1}' || true)
        if [ -z "$NAME" ]; then
            echo "未找到端口 $PORT 对应的网关容器"; exit 0
        fi
        echo "==> 摘除 $NAME (端口 $PORT)"
        sed -i "/    server $NAME:10086/d" nginx.conf
        docker exec nginx_lb nginx -s reload || true
        docker rm -f "$NAME"
        echo "==> 已移除"
        ;;
    status)
        echo "==> 当前网关实例:"
        docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' | grep -E "agent_gateway|NAMES"
        echo ""
        echo "==> nginx upstream:"
        grep -A6 "upstream gateway_cluster" nginx.conf
        ;;
    *)
        echo "用法: bash scale_gateway.sh {add|remove|status} [port]" >&2
        exit 1
        ;;
esac
