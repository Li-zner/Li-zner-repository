#!/bin/bash
# ============================================================
# 一键启动脚本（骨架；复制后按项目改服务名与端口）
#
# 架构模型：默认拉起单实例 + Nginx + 自动伸缩调节器，
#           其余实例由 autoscaler 按 QPS 动态扩/缩。
# 用法:
#   bash start_all.sh                 # 单实例 + 自动伸缩
#   bash start_all.sh --all           # 全量实例
#   bash start_all.sh --no-autoscale  # 只拉服务
# 注意: 必须 LF 换行；基础设施步骤容错（已运行不中断）
# ============================================================
set -euo pipefail

log() { echo "[$(date '+%F %T')] $*"; }
ok()  { echo "[OK] $*"; }

ALL_MODE=0
AUTOSCALE=1

for arg in "$@"; do
    case "$arg" in
        --all) ALL_MODE=1 ;;
        --no-autoscale) AUTOSCALE=0 ;;
        *) echo "[错误] 未知参数: $arg" >&2; exit 1 ;;
    esac
done

ALL_SERVICES=(app app2 app3 app4)
SINGLE_SERVICE=(app)

# ===== 基础设施（容错：postgres/redis 已在运行则继续）=====
log "启动基础设施（postgres / redis / 监控）..."
if ! docker compose up -d --no-recreate postgres redis; then
    echo "[警告] 基础设施部分启动失败（若已在运行可忽略）" >&2
else
    ok "基础设施就绪"
fi

# ===== 网关实例 =====
if [ "$ALL_MODE" = "1" ]; then
    docker compose up -d "${ALL_SERVICES[@]}"
    RUN_SERVICES=("${ALL_SERVICES[@]}")
else
    docker compose up -d "${SINGLE_SERVICE[@]}"
    docker compose stop app2 app3 app4 || true
    RUN_SERVICES=("${SINGLE_SERVICE[@]}")
fi

# ===== 健康检查等待 =====
for svc in "${RUN_SERVICES[@]}"; do
    log "等待 $svc 健康..."
    for i in $(seq 1 60); do
        if docker inspect --format '{{.State.Health.Status}}' "$(docker compose ps -q "$svc")" 2>/dev/null | grep -q healthy; then
            break
        fi
        sleep 2
    done
    ok "$svc 健康"
done

# ===== Nginx（--no-deps 防连动已停实例）=====
docker compose up -d --no-deps nginx
ok "Nginx 已启动"

# ===== 同步 upstream 与运行实例（如 autoscale.sh 有 reconcile）=====
if [ -f scripts/autoscale.sh ]; then
    bash scripts/autoscale.sh reconcile >/dev/null 2>&1 || true
fi

# ===== 自动伸缩 =====
if [ "$AUTOSCALE" = "1" ] && [ -f scripts/autoscale.sh ]; then
    bash scripts/autoscale.sh start || echo "[警告] autoscale 启动失败（不影响主服务）" >&2
fi
ok "完成"
