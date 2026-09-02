#!/bin/bash
# ============================================================
# 一键启动脚本 - agent_gateway
#
# 架构模型：默认只拉起 1 个基础实例 + Nginx，其余实例由自动伸缩
#           调节器按 QPS 动态扩容/缩容（见 scripts/autoscale.sh）
#
# 使用方法:
#   bash start_all.sh                 # 拉起单实例 + 自动伸缩调节器
#   bash start_all.sh --all           # 拉起全部 4 实例 + 调节器（手动模式）
#   bash start_all.sh --no-autoscale  # 只拉服务，不启动调节器
#   bash start_all.sh --build         # 强制重建镜像（默认跳过）
#
# 隧道请自行拉起（frp/ngrok/cloudflared 等）
# 注意: 必须用 LF 换行（Windows 编辑器勿改 CRLF）
# ============================================================

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

GATEWAY_IMAGE="agent_gateway-gateway:latest"
HEALTH_TIMEOUT=150
NGINX_PORT="10090"

# 容器名 -> 健康检查端口
declare -A HEALTH_PORTS=(
  ["agent_gateway"]="10092"
  ["agent_gateway2"]="10089"
  ["agent_gateway3"]="10093"
  ["agent_gateway4"]="10094"
)
ALL_SERVICES=("gateway" "gateway2" "gateway3" "gateway4")
SINGLE_SERVICE=("gateway")

# 解析参数: --all 全量实例 / --build 强制重建 / --no-autoscale 不启动自动伸缩
ALL_MODE=0
BUILD_MODE=0
AUTOSCALE=1
for arg in "$@"; do
    case "$arg" in
        --all)           ALL_MODE=1 ;;
        --build)         BUILD_MODE=1 ;;
        --no-autoscale)  AUTOSCALE=0 ;;
        *)
            echo "[❌] 未知参数: $arg（支持 --all / --build / --no-autoscale）" >&2
            exit 1
            ;;
    esac
done

log() { echo -e "\033[1;36m==> $*\033[0m"; }
ok()  { echo -e "\033[1;32m    OK: $*\033[0m"; }

# ---------- 等待单个容器健康（Docker HEALTHCHECK + HTTP 双确认） ----------
wait_healthy() {
    local container="$1"
    local port="$2"
    local deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        local status
        status=$(docker inspect --format='{{.State.Health.Status}}' "$container" 2>/dev/null || echo "starting")
        if [ "$status" = "healthy" ] && curl -sf "http://localhost:${port}/health" >/dev/null 2>&1; then
            return 0
        fi
        sleep 3
    done
    return 1
}

# ---------- 预检：基础设施容器归属 ----------
# 防止非 compose 启动的 postgres/redis 占用容器名，导致网关连错实例（认证失败→unhealthy）。
# 本项目 compose project 名为 agent_gateway；若同名容器不属于该项目则判定为冲突。
COMPOSE_PROJECT="agent_gateway"
check_infra_ownership() {
    local name="$1"
    local cid
    cid=$(docker ps -a --filter "name=^/${name}$" --format '{{.ID}}' | head -n1)
    [ -z "$cid" ] && return 0   # 该名字未被占用，无需检查
    local proj
    proj=$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}' "$cid" 2>/dev/null || echo "")
    if [ "$proj" != "$COMPOSE_PROJECT" ]; then
        echo "[⚠️] 发现非 compose 容器占用了 \"${name}\" 名称（不属于 ${COMPOSE_PROJECT} 项目）！" >&2
        echo "      这会让网关连错实例而 unhealthy。请先处理它（容器名: ${name}，ID: ${cid}）：" >&2
        echo "        docker rm -f ${cid}" >&2
        return 1
    fi
    return 0
}

echo "============================================"
echo "  🚀 一键启动所有服务"
echo "============================================"
echo ""

# ===== 0. 检查 .env（compose 依赖）=====
if [ ! -f ".env" ]; then
    echo "[❌] 缺少 .env 文件，请先复制 .env.example 并填写配置" >&2
    exit 1
fi

# ===== 1. 检查 Docker =====
if ! docker info > /dev/null 2>&1; then
    echo "[❌] Docker 未运行，请先启动 Docker Desktop" >&2
    exit 1
fi
ok "Docker 运行中"

# ===== 2. 构建网关镜像 =====
# 默认跳过构建直接启动（pip+torch 全量重装约 5 分钟，不适合每次启动都跑）
# 只有镜像不存在，或显式传入 --build 时才重建
if [ "$BUILD_MODE" = "1" ]; then
    log "强制重新构建镜像（层缓存优先）..."
    docker build -t "$GATEWAY_IMAGE" .
elif ! docker image inspect "$GATEWAY_IMAGE" >/dev/null 2>&1; then
    log "镜像不存在，首次构建（pip 依赖较重，请耐心等待）..."
    docker build -t "$GATEWAY_IMAGE" .
else
    ok "使用现有镜像 $GATEWAY_IMAGE（如需重新构建请加 --build）"
fi

# ===== 3. 启动基础设施（PostgreSQL / Redis / 监控）=====
# 显式列出服务，避免不带服务名的 up -d 把停掉的网关也拉起来
# 注意：若 postgres/redis 已由其他方式启动（compose 与手动容器命名冲突），
#       此处失败不阻断——基础设施已在运行即可继续

# 3.0 预检：postgres/redis 必须属于本 compose 项目，否则网关会连错实例而 unhealthy
log "预检基础设施容器归属（postgres / redis）..."
INFRA_OK=1
check_infra_ownership postgres || INFRA_OK=0
check_infra_ownership redis   || INFRA_OK=0
if [ "$INFRA_OK" = "0" ]; then
    echo "[❌] 检测到冲突的基础设施容器，请按上方提示处理后重新运行。为保护数据，脚本已中止。" >&2
    exit 1
fi
ok "基础设施容器归属正常"

log "启动基础设施（postgres / redis / prometheus / grafana / loki / tempo ...）"
if ! docker compose up -d --no-recreate postgres redis prometheus grafana loki tempo alertmanager promtail; then
    echo "[⚠️] 基础设施部分启动失败（若 postgres/redis 已在运行可忽略）" >&2
else
    ok "基础设施已就绪"
fi

# ===== 4. 启动网关实例 =====
# 默认：只起 1 个基础实例（gateway/10092）+ Nginx，其余由自动伸缩按 QPS 扩缩
if [ "$ALL_MODE" = "1" ]; then
    log "--all 模式：手动启动全部 4 个网关实例 (10092/10089/10093/10094)..."
    docker compose up -d "${ALL_SERVICES[@]}"
    RUN_SERVICES=("${ALL_SERVICES[@]}")
else
    log "默认模式：仅启动基础实例 gateway(10092)，其余交给自动伸缩..."
    docker compose up -d "${SINGLE_SERVICE[@]}"
    docker compose stop gateway2 gateway3 gateway4 || true
    RUN_SERVICES=("${SINGLE_SERVICE[@]}")
fi

# ===== 5. 健康检查等待 =====
for svc in "${RUN_SERVICES[@]}"; do
    container="agent_gateway${svc#gateway}"
    port="${HEALTH_PORTS[$container]}"
    log "等待 $container ($port) 健康..."
    if ! wait_healthy "$container" "$port"; then
        echo "[❌] $container 未通过健康检查，请查看日志: docker logs $container" >&2
        exit 1
    fi
    ok "$container 健康"
done

# ===== 6. 启动 Nginx 负载均衡 =====
# --no-deps: nginx 的 depends_on 会连带启动被停掉的网关实例，必须跳过依赖
log "启动 Nginx 负载均衡 ($NGINX_PORT)..."
docker compose up -d --no-deps nginx
if ! curl -sf "http://localhost:${NGINX_PORT}/health" >/dev/null 2>&1; then
    echo "[❌] Nginx 负载均衡未就绪" >&2
    exit 1
fi
ok "Nginx 已启动 (http://localhost:${NGINX_PORT})"

# 同步 nginx upstream 与当前运行实例（Docker DNS 不解析已停服务，reload 会失败）
log "同步 nginx upstream 与运行实例..."
bash scripts/autoscale.sh reconcile >/dev/null 2>&1 || true

# ===== 7. 启动自动伸缩调节器 =====
if [ "$AUTOSCALE" = "1" ]; then
    log "启动自动伸缩调节器（按 QPS 自动扩/缩 gateway2-4，日志: autoscale.log）..."
    if bash scripts/autoscale.sh start; then
        ok "自动伸缩调节器已启动（查看: bash scripts/autoscale.sh status）"
    else
        echo "[⚠️] 自动伸缩调节器启动失败，不影响主服务" >&2
    fi
else
    ok "已跳过自动伸缩调节器（--no-autoscale）"
fi

# ===== 8. 验证状态 =====
echo ""
echo "============================================"
echo "  📋 当前运行状态"
echo "============================================"
echo ""
docker compose ps

echo ""
echo "============================================"
if [ "$ALL_MODE" = "1" ]; then
    echo "  📌 直连实例1: http://localhost:10092"
    echo "  📌 直连实例2: http://localhost:10089"
    echo "  📌 直连实例3: http://localhost:10093"
    echo "  📌 直连实例4: http://localhost:10094"
else
    echo "  📌 直连实例:  http://localhost:10092"
fi
echo "  📌 负载均衡: http://localhost:10090"
echo "  📌 全部实例: bash start_all.sh --all"
echo "============================================"
