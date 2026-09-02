#!/bin/bash
# ============================================================
# autoscale.sh — 网关实例自动伸缩调节器
#
# 原理: 周期性从 Prometheus 查询网关总 QPS（req/s），按阈值
#       自动扩展/收缩 gateway2/gateway3/gateway4 实例：
#   - QPS 连续 UP_HITS 次   >  HIGH_QPS  → 扩容一个实例（最多 MAX）
#   - QPS 连续 DOWN_HITS 次 <  LOW_QPS  → 缩容一个实例（至少 MIN）
#   - 每次伸缩后冷却 COOLDOWN 秒，防止抖动
#
# 前置条件:
#   - Prometheus 能采集网关 /metrics（prometheus.yml 必须配置
#     agent_gateway*:10086，不能是旧的 gw_10092:10086）
#   - 依赖 docker / curl / awk / grep / sed（Git Bash 自带）
#
# 用法:
#   bash autoscale.sh start [--interval 30] [--high 40] [--low 12] \
#                          [--min 1] [--max 4] [--up-hits 3] \
#                          [--down-hits 6] [--cooldown 60]
#   bash autoscale.sh stop
#   bash autoscale.sh status
#   bash autoscale.sh check      # 单次采样（不循环）
#   bash autoscale.sh reconcile  # 让 nginx upstream 与运行实例对齐（幂等）
#
# 伸缩时会同步维护 nginx.conf 的 upstream（扩容加入/reload，缩容先摘除再停），
# 因为 Docker DNS 不解析已停服务，nginx reload 引用已停实例会报 host not found。
#
# 注意: 必须用 LF 换行（Windows 编辑器勿改 CRLF）
# ============================================================

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"
SCRIPT="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"

PROM_URL="${PROM_URL:-http://localhost:9090}"
PID_FILE="${TMPDIR:-/tmp}/autoscale.pid"
LOG_FILE="$PROJECT_DIR/autoscale.log"

# 默认参数（可用 --xxx 覆盖）
INTERVAL=30
HIGH_QPS=40
LOW_QPS=12
MIN_INSTANCES=1
MAX_INSTANCES=4
UP_HITS=3
DOWN_HITS=6
COOLDOWN=60

# 可伸缩实例（按扩容顺序 gateway2→3→4）
SCALEABLE_SERVICES=(gateway2 gateway3 gateway4)

svc_to_container() { echo "agent_gateway${1#gateway}"; }

log() { echo "[$(date '+%F %T')] $*"; }

# ---------- 从 Prometheus 查询当前网关总 QPS ----------
current_qps() {
    local resp val
    resp=$(curl -sf "$PROM_URL/api/v1/query" \
        --data-urlencode 'query=sum(rate(gateway_requests_total[2m]))' 2>/dev/null || true)
    val=$(echo "$resp" | grep -o '"value":\[[0-9.]*,"[^"]*"\]' | head -1 \
        | sed 's/.*,"\([^"]*\)"\]/\1/')
    [ -n "$val" ] && echo "$val" || echo "0"
}

# 浮点比较（用 awk，避免依赖 bc）
qps_gt() { awk -v a="$1" -v b="$2" 'BEGIN{exit !(a>b)}'; }
qps_lt() { awk -v a="$1" -v b="$2" 'BEGIN{exit !(a<b)}'; }

# 当前运行中的网关实例数（agent_gateway / agent_gateway2..4）
running_count() {
    docker ps --filter "name=agent_gateway" --format '{{.Names}}' 2>/dev/null \
        | grep -cE '^agent_gateway[0-9]*$' || true
}

# ---------- nginx upstream 同步（与运行实例保持一致）----------
# Docker DNS 不解析已停服务：nginx.conf 里引用已停实例会让 reload 报
# "host not found in upstream" 而失败，所以扩容/缩容必须同步维护 upstream。
# nginx.conf 以只读 bind-mount 进 nginx_lb 容器，改宿主文件 + reload 即可生效。
BASE_SERVER_LINE='    server agent_gateway:10086 max_fails=3 fail_timeout=30s;'

# 把实例加入 nginx upstream 并 reload（幂等；reload 失败自动重试等 DNS 生效）
nginx_add() {
    local cname="$1" i
    if ! grep -q "server ${cname}:10086" nginx.conf; then
        sed -i "s|${BASE_SERVER_LINE}|${BASE_SERVER_LINE}\n    server ${cname}:10086 max_fails=3 fail_timeout=30s;|" nginx.conf
        for ((i=0;i<15;i++)); do
            # 先 nginx -t 校验语法，再 reload（防 sed 改坏配置后热更新失败）
            if docker exec nginx_lb nginx -t >/dev/null 2>&1 && docker exec nginx_lb nginx -s reload 2>/dev/null; then
                log "  nginx upstream 已加入 ${cname}"
                return 0
            fi
            sleep 2
        done
        log "⚠ nginx reload 多次失败（${cname} 未解析？）"
    fi
}

# 把实例从 nginx upstream 摘除并 reload（幂等）
nginx_remove() {
    local cname="$1"
    if grep -q "server ${cname}:10086" nginx.conf; then
        sed -i "/    server ${cname}:10086/d" nginx.conf
        docker exec nginx_lb nginx -t >/dev/null 2>&1 && docker exec nginx_lb nginx -s reload 2>/dev/null \
            || log "⚠ nginx 校验/reload 失败（${cname} 摘除）"
        log "  nginx upstream 已摘除 ${cname}"
    fi
}

# 扩容一个实例（按 gateway2→3→4 顺序找第一个没在跑的）
scale_up() {
    for svc in "${SCALEABLE_SERVICES[@]}"; do
        local cname
        cname=$(svc_to_container "$svc")
        if ! docker ps --format '{{.Names}}' | grep -qx "$cname"; then
            log "⬆ 扩容: 启动 $cname"
            docker compose up -d --no-deps "$svc"
            nginx_add "$cname"
            return 0
        fi
    done
    return 1
}

# 缩容一个实例（按 gateway4→3→2 顺序停最大的编号）
scale_down() {
    local i
    for ((i=${#SCALEABLE_SERVICES[@]}-1; i>=0; i--)); do
        local svc="${SCALEABLE_SERVICES[$i]}" cname
        cname=$(svc_to_container "$svc")
        if docker ps --format '{{.Names}}' | grep -qx "$cname"; then
            log "⬇ 缩容: 摘除并停止 $cname"
            nginx_remove "$cname"   # 先摘除再停，保证 reload 能成功
            docker compose stop "$svc"
            return 0
        fi
    done
    return 1
}

# 让 nginx upstream 与当前运行实例对齐（幂等；启动后或手工改实例后调用）
reconcile_nginx() {
    local name
    # 1) 删除配置中「未在运行」的 server 行
    while IFS= read -r line; do
        name=$(echo "$line" | sed -n 's/.*server \(agent_gateway[0-9]*\):10086.*/\1/p')
        if [ -n "$name" ] && ! docker ps --format '{{.Names}}' | grep -qx "$name"; then
            sed -i "/    server ${name}:10086/d" nginx.conf
            log "  nginx 摘除 ${name}（未运行）"
        fi
    done < nginx.conf
    # 2) 加入「在运行但不在配置中」的实例（base 常驻除外）
    for name in $(docker ps --format '{{.Names}}' | grep -E '^agent_gateway[0-9]*$'); do
        if [ "$name" != "agent_gateway" ] && ! grep -q "server ${name}:10086" nginx.conf; then
            nginx_add "$name"
        fi
    done
    # 3) 最终校验 + reload，确保一致
    docker exec nginx_lb nginx -t >/dev/null 2>&1 && docker exec nginx_lb nginx -s reload 2>/dev/null \
        || log "⚠ nginx 校验/reload 失败"
    log "nginx upstream 已与运行实例对齐"
}

# ---------- 主循环 ----------
run_loop() {
    local last_action_ts=0 up_counter=0 down_counter=0
    log "自动伸缩已启动: interval=${INTERVAL}s high>${HIGH_QPS} low<${LOW_QPS} min=${MIN_INSTANCES} max=${MAX_INSTANCES} cooldown=${COOLDOWN}s"
    while true; do
        local qps now count cooled
        qps=$(current_qps)
        now=$(date +%s)
        count=$(running_count)
        log "采样: QPS=${qps} req/s 实例数=${count}（阈值: 高>${HIGH_QPS} 低<${LOW_QPS}）"

        if qps_gt "$qps" "$HIGH_QPS"; then
            up_counter=$((up_counter+1)); down_counter=0
        elif qps_lt "$qps" "$LOW_QPS"; then
            down_counter=$((down_counter+1)); up_counter=0
        else
            up_counter=0; down_counter=0
        fi

        cooled=0
        [ $((now - last_action_ts)) -ge "$COOLDOWN" ] && cooled=1

        if [ "$cooled" = 1 ] && [ "$up_counter" -ge "$UP_HITS" ] && [ "$count" -lt "$MAX_INSTANCES" ]; then
            if scale_up; then last_action_ts=$(date +%s); fi
            up_counter=0
        elif [ "$cooled" = 1 ] && [ "$down_counter" -ge "$DOWN_HITS" ] && [ "$count" -gt "$MIN_INSTANCES" ]; then
            if scale_down; then last_action_ts=$(date +%s); fi
            down_counter=0
        fi

        sleep "$INTERVAL"
    done
}

parse_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --interval)   INTERVAL="$2";      shift 2 ;;
            --high)       HIGH_QPS="$2";      shift 2 ;;
            --low)        LOW_QPS="$2";       shift 2 ;;
            --min)        MIN_INSTANCES="$2"; shift 2 ;;
            --max)        MAX_INSTANCES="$2"; shift 2 ;;
            --up-hits)    UP_HITS="$2";       shift 2 ;;
            --down-hits)  DOWN_HITS="$2";     shift 2 ;;
            --cooldown)   COOLDOWN="$2";      shift 2 ;;
            *) echo "未知参数: $1" >&2; exit 1 ;;
        esac
    done
    if [ "$MAX_INSTANCES" -lt "$MIN_INSTANCES" ]; then
        echo "[❌] --max 不能小于 --min" >&2; exit 1
    fi
}

is_running() {
    [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null
}

case "${1:-start}" in
    start)
        shift || true
        parse_args "$@"
        if is_running; then
            echo "autoscale 已在运行 (PID $(cat "$PID_FILE"))，日志: $LOG_FILE"
            exit 0
        fi
        # 后台脱离终端运行，输出写入日志
        nohup "$SCRIPT" loop "$@" >> "$LOG_FILE" 2>&1 &
        echo $! > "$PID_FILE"
        echo "✅ autoscale 已启动 (PID $(cat "$PID_FILE"))"
        echo "   日志: $LOG_FILE"
        echo "   查看: bash autoscale.sh status"
        ;;
    loop)
        shift || true
        parse_args "$@"
        run_loop
        ;;
    stop)
        if is_running; then
            kill "$(cat "$PID_FILE")"
            rm -f "$PID_FILE"
            echo "✅ autoscale 已停止"
        else
            rm -f "$PID_FILE"
            echo "autoscale 未在运行"
        fi
        ;;
    status)
        if is_running; then
            echo "✅ autoscale 运行中 (PID $(cat "$PID_FILE"))"
        else
            echo "❌ autoscale 未运行"
        fi
        echo "当前 QPS: $(current_qps) req/s"
        echo "当前实例数: $(running_count)"
        echo "--- 最近日志 ---"
        [ -f "$LOG_FILE" ] && tail -n 8 "$LOG_FILE" || echo "(无日志)"
        ;;
    check)
        shift || true
        parse_args "$@"
        echo "QPS=$(current_qps) req/s  实例数=$(running_count)  (高>${HIGH_QPS} 低<${LOW_QPS})"
        ;;
    reconcile)
        shift || true
        parse_args "$@"
        reconcile_nginx
        ;;
    *)
        echo "用法: bash autoscale.sh {start|stop|status|check} [--interval 30] [--high 40] [--low 12] [--min 1] [--max 4] [--up-hits 3] [--down-hits 6] [--cooldown 60]" >&2
        exit 1
        ;;
esac
