#!/bin/bash
# ============================================================
# deploy_aliyun.sh —— 阿里云轻量服务器(2C1G)一键部署 agent_gateway 精简版
# 在实例上执行（代码已通过 git clone 或上传位于当前目录）
#
# 用法:
#   在实例根目录执行: bash deploy/deploy_aliyun.sh
#   敏感值可先用环境变量传入（避免写入对话/历史）:
#     export DSH_ADMIN_PASSWORD='...' DSH_DEEPSEEK_API_KEY='...'
#   或部署后手动编辑 deploy/.env.lite
# ============================================================
set -euo pipefail

cd "$(dirname "$0")/.."   # 仓库根（假定 clone 后目录结构完整）

echo "=== 1/4 生成 deploy/.env.lite ==="
cp deploy/.env.lite.example deploy/.env.lite
# 随机生成会话/密钥（openssl）
sed -i "s|^SESSION_SECRET_KEY=.*|SESSION_SECRET_KEY=$(openssl rand -hex 32)|" deploy/.env.lite
sed -i "s|^JWT_SECRET=.*|JWT_SECRET=$(openssl rand -hex 32)|" deploy/.env.lite
# admin 密码 / DeepSeek key：从环境变量取，未设则保持原占位（部署前必须手动填）
if [ -n "${DSH_ADMIN_PASSWORD:-}" ]; then
    sed -i "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=$DSH_ADMIN_PASSWORD|" deploy/.env.lite
fi
if [ -n "${DSH_DEEPSEEK_API_KEY:-}" ]; then
    sed -i "s|^DEEPSEEK_API_KEY=.*|DEEPSEEK_API_KEY=$DSH_DEEPSEEK_API_KEY|" deploy/.env.lite
fi
echo "  已生成。请确认必填项（ADMIN_PASSWORD / DEEPSEEK_API_KEY）："
grep -E "^(ADMIN_PASSWORD|DEEPSEEK_API_KEY)=" deploy/.env.lite | sed 's/=.*/=<已填或待填>/'

echo "=== 2/4 构建精简镜像（去 torch/tesseract，约 500MB）==="
docker compose -f deploy/docker-compose.lite.yml build 2>&1 | tail -3

echo "=== 3/4 启动容器 ==="
docker compose -f deploy/docker-compose.lite.yml up -d

echo "=== 4/4 健康检查（等待网关就绪）==="
for i in $(seq 1 15); do
    if curl -sf http://localhost:10090/health >/dev/null 2>&1; then
        echo "  ✅ nginx 入口 /health OK (10s 后验证业务)"
        break
    fi
    sleep 2
done
sleep 8
echo "  网关 /health:"; curl -sf http://localhost:10090/health || echo "  （未通过，docker logs agent_gateway 排查）"
echo ""
echo "部署完成。公网入口: http://39.108.227.214:10090（实例公网 IP）"
echo "安全提醒: 请确认 deploy/.env.lite 无明文敏感值残留；/api 接口已限流；如需 HTTPS 再配证书"
