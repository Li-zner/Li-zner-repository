#!/bin/bash
# ============================================================
# JWT 密钥轮换演练（双 Key 并行期）
#
# 流程:
#   1) 生成新随机密钥
#   2) 旧密钥保留为 JWT_SECRET_OLD（并行期：旧 token 仍可验证）
#   3) 更新 JWT_SECRET 为新的
#   4) 重建网关容器 → 新 token 用新密钥，旧 token 用旧密钥
#   5) 并行期结束（旧 token 全过期）后移除 JWT_SECRET_OLD 再重建
#
# 用法:
#   bash rotate_jwt_secret.sh            # 真实轮换
#   bash rotate_jwt_secret.sh --dry-run  # 演练（只打印不修改）
#   bash rotate_jwt_secret.sh --check    # 检查当前并行期状态
#   bash rotate_jwt_secret.sh --remove-old  # 结束并行期（移除旧密钥）
#
# 注意: 需要 root 写 /etc/agent_gateway/.env；必须 LF 换行
# ============================================================
set -euo pipefail

ENV_FILE="/etc/agent_gateway/.env"

log() { echo "[$(date '+%F %T')] $*"; }

gen_secret() { openssl rand -hex 32; }

current_secret() {
    grep '^JWT_SECRET=' "$ENV_FILE" 2>/dev/null | cut -d= -f2 || true
}

check() {
    if [ ! -f "$ENV_FILE" ]; then
        echo "❌ 未找到 $ENV_FILE" >&2
        exit 1
    fi
    if grep -q '^JWT_SECRET_OLD=' "$ENV_FILE"; then
        echo "✅ 并行期: JWT_SECRET_OLD 存在（新旧 token 均可验证）"
    else
        echo "ℹ️  非并行期: 未设置 JWT_SECRET_OLD"
    fi
    echo "JWT_SECRET 长度: $(current_secret | wc -c)"
}

rotate() {
    [ "$(id -u)" = "0" ] || { echo "❌ 需要 root 执行（wsl -u root）" >&2; exit 1; }
    check

    # 备份整个 env 文件（轮换前）
    cp "$ENV_FILE" "${ENV_FILE}.bak-$(date +%Y%m%d_%H%M%S)"
    log "已备份 env 文件"

    OLD=$(current_secret)
    NEW=$(gen_secret)
    log "生成新密钥（长度 ${#NEW}）"

    # 更新 JWT_SECRET 为新的，并清理旧 JWT_SECRET_OLD 行后追加
    sed -i "s|^JWT_SECRET=.*|JWT_SECRET=${NEW}|" "$ENV_FILE"
    sed -i "/^JWT_SECRET_OLD=/d" "$ENV_FILE"
    if [ -n "$OLD" ]; then
        # 确保文件以换行结尾（否则追加会拼到上一行尾部，损坏配置）
        if [ -n "$(tail -c 1 "$ENV_FILE" | tr -d '\n')" ]; then
            echo "" >> "$ENV_FILE"
        fi
        echo "JWT_SECRET_OLD=${OLD}" >> "$ENV_FILE"
        log "旧密钥已保留为 JWT_SECRET_OLD（并行期开始）"
    fi

    echo ""
    echo "========================================"
    echo "  轮换完成！下一步:"
    echo "  1) 重建网关: docker compose up -d --force-recreate gateway"
    echo "  2) 验证: 旧登录态仍可用（旧密钥验旧 token）+ 新登录正常"
    echo "  3) 并行期过后执行: bash rotate_jwt_secret.sh --remove-old"
    echo "========================================"
}

remove_old() {
    [ "$(id -u)" = "0" ] || { echo "❌ 需要 root 执行" >&2; exit 1; }
    sed -i "/^JWT_SECRET_OLD=/d" "$ENV_FILE"
    echo "✅ 已移除 JWT_SECRET_OLD（并行期结束）"
    echo "   重建网关后旧 token 将失效（到期即拒）"
}

case "${1:-rotate}" in
    rotate)      rotate ;;
    --dry-run)   echo "== 演练模式：仅展示将执行的操作 =="; check ;;
    --check)     check ;;
    --remove-old) remove_old ;;
    *) echo "用法: bash $0 [rotate|--dry-run|--check|--remove-old]" >&2; exit 1 ;;
esac
