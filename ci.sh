#!/bin/bash
# ============================================================
# ci.sh — 本地 CI/CD 流水线
#
# 流程: [1] 语法检查 → [1.5] 单元测试 → [2] 构建(层缓存) → [3] 滚动部署(逐个实例)
#       → [4] 全链路冒烟测试 → [5] 完成；任何一步失败自动回滚
#
# 用法:
#   bash ci.sh               # 完整流水线（检查+构建+部署+冒烟）
#   bash ci.sh --no-deploy   # 只做检查+构建，不部署
#   bash ci.sh --rollback <镜像ID>   # 回滚到指定镜像
#
# 注意: 必须用 LF 换行（Windows 编辑器勿改 CRLF）
# ============================================================

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

GATEWAY_IMAGE="agent_gateway-gateway:latest"
COMMIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo 'local')"
BUILD_TAG="agent_gateway-gateway:${COMMIT_SHA}"

# 优先使用项目 venv 的 Python（保证 pytest/asyncpg 等依赖存在；否则 PATH 里的
# python 可能是其他环境，导致 pytest 步骤 ModuleNotFoundError）
if [ -x "$PROJECT_DIR/.venv/Scripts/python.exe" ]; then
    PYTHON="$PROJECT_DIR/.venv/Scripts/python.exe"
elif [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
    PYTHON="$PROJECT_DIR/.venv/bin/python"
else
    PYTHON="python"
fi

GATEWAY_SERVICES=("gateway" "gateway2" "gateway3" "gateway4")
# 容器名与健康检查端口映射
declare -A HEALTH_PORTS=(
  ["gateway"]="10092"
  ["gateway2"]="10089"
  ["gateway3"]="10093"
  ["gateway4"]="10094"
)
NGINX_PORT="10090"
HEALTH_TIMEOUT=150

log()  { echo -e "\033[1;36m==> $*\033[0m"; }
ok()   { echo -e "\033[1;32m    OK: $*\033[0m"; }

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

# ---------- 记录当前运行镜像（用于回滚） ----------
get_current_image() {
    docker inspect --format='{{.Image}}' agent_gateway 2>/dev/null || echo ""
}

# ---------- 回滚到指定镜像 ----------
rollback() {
    local image_id="$1"
    log "回滚到镜像 $image_id"
    docker tag "$image_id" "$GATEWAY_IMAGE" >/dev/null 2>&1 || true
    for svc in "${GATEWAY_SERVICES[@]}"; do
        docker compose up -d --no-deps --force-recreate "$svc" >/dev/null 2>&1 || true
    done
    log "回滚完成，请手动确认健康状态"
}

# ============================================================
# [0] 回滚模式
# ============================================================
if [ "${1:-}" = "--rollback" ]; then
    PREV_IMAGE="${2:-}"
    if [ -z "$PREV_IMAGE" ]; then
        echo "用法: bash ci.sh --rollback <镜像ID>" >&2
        exit 1
    fi
    rollback "$PREV_IMAGE"
    exit 0
fi

# ============================================================
# [1] 语法检查（AST 纯解析，不写 .pyc，兼容 WSL/Windows 任意 python）
# ============================================================
log "[1/5] Python 语法检查"
"$PYTHON" -B -c "
import ast, pathlib, sys
errors = []
for p in pathlib.Path('app').rglob('*.py'):
    try:
        ast.parse(p.read_text(encoding='utf-8'))
    except SyntaxError as e:
        errors.append(f'{p}:{e.lineno}: {e.msg}')
    except Exception as e:
        errors.append(f'{p}: {e}')
if errors:
    sys.stderr.write('\n'.join(errors) + '\n')
    sys.exit(1)
print('    OK: app/ 语法检查通过')
" || { echo "    FAIL: Python 语法检查未通过" >&2; exit 1; }
ok "app/ 语法检查通过"

# ============================================================
# [1.4] 编码规范门禁（函数 ≤80 行 / 文件 ≤600 行；历史债文件豁免，见
#       wiki 10-Rules/08-Ponytail-编码规则.md 硬规则说明）
# ============================================================
log "[1.4] 编码规范检查 (check_code_rules.py)"
if "$PYTHON" -B scripts/check_code_rules.py \
    --skip-files service.py,main.py,runner.py,router.py,routing_table.py,map_api.py,tools.py,db_maintenance.py,cdc_acceptance.py,final_setup.py \
    --no-silent; then
    ok "编码规范检查通过（新增代码函数 ≤80 行 / 文件 ≤600 行）"
else
    echo "    FAIL: 新增/修改代码违反编码规范（函数超 80 行或文件超 600 行）" >&2
    exit 1
fi

# ============================================================
# [1.5] 单元测试（pytest，仅离线单测；集成测试需运行实例手动跑）
# ============================================================
log "[1.5] 单元测试 (pytest tests/unit)"
if ! "$PYTHON" -m pytest -q; then
    echo "    FAIL: 单元测试未通过（如未安装 pytest: pip install -r requirements-dev.txt）" >&2
    exit 1
fi
ok "单元测试通过"

# ============================================================
# [1.6] 集成测试（可选：实例在运行时才执行，否则跳过不阻断）
# ============================================================
log "[1.6] 集成测试（可选，需运行实例）"
if curl -sf "http://localhost:${NGINX_PORT}/health" >/dev/null 2>&1; then
    if "$PYTHON" -m pytest tests/integration -m integration -q; then
        ok "集成测试通过"
    else
        echo "    FAIL: 集成测试未通过" >&2
        exit 1
    fi
else
    echo "    [提示] 实例未运行，跳过集成测试"
fi

# ============================================================
# [1.7] 评测门禁（可选：需实例运行 + 进程环境变量 DEEPSEEK_API_KEY；
#       快速模式 20 条控成本；低于阈值 → 阻断发布）
# ============================================================
log "[1.7] 评测门禁（可选，需实例运行 + DEEPSEEK_API_KEY）"
if curl -sf "http://localhost:${NGINX_PORT}/health" >/dev/null 2>&1 && [ -n "${DEEPSEEK_API_KEY:-}" ]; then
    if "$PYTHON" -B tests/run_evaluation.py --max-cases 20 --threshold 8.0 --no-cache; then
        ok "评测门禁通过"
    else
        echo "    FAIL: 评测门禁未通过（整体/分类平均分低于阈值，见上面报告）" >&2
        exit 1
    fi
else
    echo "    [提示] 实例未运行或 DEEPSEEK_API_KEY 不在进程环境变量，跳过评测门禁（手动跑: python tests/run_evaluation.py）"
fi

# ============================================================
# [2] 构建镜像（层缓存优先；依赖未变时只重建代码层）
# ============================================================
log "[2/5] 构建镜像（层缓存优先）"
PREV_IMAGE="$(get_current_image)"
if ! docker build -t "$BUILD_TAG" -t "$GATEWAY_IMAGE" .; then
    echo "    FAIL: 镜像构建失败" >&2
    exit 1
fi
ok "构建完成: $BUILD_TAG"

# --no-deploy 模式：到此为止
if [ "${1:-}" = "--no-deploy" ]; then
    ok "构建完成（未部署）: $BUILD_TAG"
    exit 0
fi

# ============================================================
# [3] 滚动部署：一次只重建一个实例，健康后才动下一个
#     避免 deploy.sh 的 --force-recreate 四实例同时停摆
# ============================================================
log "[3/5] 滚动部署（逐个实例，健康后再动下一个）"
for svc in "${GATEWAY_SERVICES[@]}"; do
    container="agent_gateway${svc#gateway}"
    port="${HEALTH_PORTS[$svc]}"
    log "  部署 $svc ($container : $port)"
    docker compose up -d --no-deps --force-recreate "$svc" >/dev/null 2>&1 || {
        echo "    FAIL: $svc 重建失败，触发回滚" >&2
        rollback "$PREV_IMAGE"
        exit 1
    }
    if ! wait_healthy "$container" "$port"; then
        echo "    FAIL: $svc 部署后未通过健康检查，触发回滚" >&2
        rollback "$PREV_IMAGE"
        exit 1
    fi
    ok "$svc 健康"
done

# ============================================================
# [4] 全链路冒烟测试（经 nginx 负载均衡）
# ============================================================
log "[4/5] 全链路冒烟测试（经 nginx:${NGINX_PORT}）"
if ! curl -sf "http://localhost:${NGINX_PORT}/health" >/dev/null 2>&1; then
    echo "    FAIL: nginx /health 检查失败，触发回滚" >&2
    rollback "$PREV_IMAGE"
    exit 1
fi
# 登录冒烟：任意账号应返回 401/400/403（链路通）而非 5xx（服务故障）
LOGIN_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST "http://localhost:${NGINX_PORT}/api/login" \
    -H "Content-Type: application/json" \
    -d '{"username":"ci_probe","password":"ci_probe"}')
case "$LOGIN_CODE" in
    200|401|400|403) ok "登录链路正常 (HTTP $LOGIN_CODE)" ;;
    *)
        echo "    FAIL: 登录链路异常 (HTTP $LOGIN_CODE)，触发回滚" >&2
        rollback "$PREV_IMAGE"
        exit 1
        ;;
esac

# ============================================================
# [5] 完成
# ============================================================
log "[5/5] 部署完成"
echo "  镜像: $BUILD_TAG"
echo "  实例: gateway(10092) gateway2(10089) gateway3(10093) gateway4(10094)"
echo "  负载均衡: http://localhost:${NGINX_PORT}"
echo "  回滚命令: bash ci.sh --rollback $PREV_IMAGE"
