#!/bin/bash
# ============================================================
# .env 安全迁移脚本
# 将 .env 迁到 /etc/agent_gateway/（root 所有/写，644 可读），作为唯一权威配置源
# 桌面快捷方式：Agent网关配置.env.lnk → scripts/edit_env.vbs（管理员记事本）
#
# 权限说明：用 644 而非 600 —— compose CLI / docker CLI / 宿主脚本都以普通用户
# 经 UNC(\\wsl.localhost) 读取该文件，600 会全部 PermissionError；
# 单用户 WSL 环境下 root 拥有+可写已足够防篡改。多用户环境应改用 Docker Secrets。
#
# 当前架构（2026-08 已对齐）：
#   - docker-compose.yml 的 env_file 已指向 \\wsl.localhost\Ubuntu\etc\agent_gateway\.env
#   - _env.py 已支持 UNC 读取该文件（宿主机脚本无需密钥放根目录）
#   - 根目录 .env 仅剩占位注释，不再存放密钥
# ============================================================
set -e

ENV_SRC="/mnt/d/桌面/agent_gateway/.env"
ENV_DIR="/etc/agent_gateway"
ENV_DEST="${ENV_DIR}/.env"

if [ ! -f "$ENV_SRC" ] || [ -z "$(grep -v '^#' "$ENV_SRC" | grep '=')" ]; then
  echo "⚠️  根目录 .env 已无密钥（已迁移）。如需重建权威配置，请先写回密钥到 $ENV_SRC"
fi

echo "🔄 迁移 .env 到系统安全目录..."

# 创建目标目录
sudo mkdir -p "$ENV_DIR"

# 复制 .env（保留原文件作为备份）
sudo cp "$ENV_SRC" "$ENV_DEST"

# 设置权限：root 所有/写，644 可读（600 会挡掉 compose/docker/宿主脚本的 UNC 读取）
sudo chmod 644 "$ENV_DEST"
sudo chown root:root "$ENV_DEST"

echo "✅ .env 已迁移到 ${ENV_DEST}"
echo "   权限: $(stat -c '%a' "$ENV_DEST") (root 所有/写，644 可读)"

# 在 WSL 中创建软链接方便编辑
sudo ln -sf "$ENV_DEST" "/home/$(whoami)/.env.agent_gateway"
echo "🔗 软链接: ~/.env.agent_gateway → ${ENV_DEST}"

# 创建 Windows 桌面快捷方式（通过 edit_env.vbs 以管理员记事本打开）
DESKTOP="$(cmd.exe /c "echo %USERPROFILE%" 2>/dev/null | tr -d '\r')"
if [ -z "$DESKTOP" ]; then DESKTOP="/mnt/d/桌面"; fi
DESKTOP_LNK="${DESKTOP}/Agent网关配置.env.lnk"
if [ -f "/mnt/d/桌面/Agent网关配置.env.lnk" ]; then
  echo "📝 桌面快捷方式已存在: Agent网关配置.env.lnk → scripts/edit_env.vbs"
else
  echo "📝 快捷方式缺失：请运行 edit_env.vbs 或在桌面创建指向 scripts/edit_env.vbs 的快捷方式"
fi

echo ""
echo "========================================"
echo "  后续操作"
echo "========================================"
echo "1. 编辑配置: 双击桌面「Agent网关配置.env.lnk」（管理员记事本）"
echo "   或: sudo nano ${ENV_DEST}"
echo "   或: code ${ENV_DEST} (VS Code，需 sudo)"
echo ""
echo "2. 生效链路（已全部指向安全目录）:"
echo "   - docker-compose.yml env_file = UNC 路径 ✅"
echo "   - _env.py（宿主机脚本）✅"
echo "   - scripts/scale_gateway.sh --env-file ✅"
echo "   改完配置后需重建容器: docker compose up -d --force-recreate gateway"
echo "========================================"
