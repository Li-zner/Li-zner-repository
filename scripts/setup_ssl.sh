#!/bin/bash
# ============================================================
# setup_ssl.sh — HTTPS 证书自动化（Let's Encrypt + certbot）
#
# 前提:
#   1. 有域名且 DNS A 记录已指向本机公网 IP
#   2. Nginx 已对外暴露 80/443 端口
#   3. docker-compose 的 nginx 挂载了 certs 目录
#
# 用法:
#   bash setup_ssl.sh your-domain.com          # 首次签发
#   bash setup_ssl.sh --renew                  # 手动续期
#
# 自动续期: 脚本会写入 crontab（每月 1/15 号 3:00 检查续期）
# ============================================================

set -euo pipefail

DOMAIN="${1:-}"
MODE="${1:-issue}"

if [ "$MODE" = "--renew" ]; then
    echo "==> 续期证书..."
    docker run --rm -v nginx_certs:/etc/letsencrypt \
        certbot/certbot renew --quiet
    docker exec nginx_lb nginx -s reload 2>/dev/null || true
    echo "==> 续期完成"
    exit 0
fi

if [ -z "$DOMAIN" ]; then
    echo "用法: bash setup_ssl.sh your-domain.com | bash setup_ssl.sh --renew" >&2
    exit 1
fi

echo "==> 为 $DOMAIN 签发 Let's Encrypt 证书（webroot 方式）..."
echo "==> 前置: 确认 DNS A 记录 $DOMAIN 指向本机，且 nginx 已监听 80 端口"

# 1. 先启动一个临时 certbot 容器做 http-01 验证（webroot 挂载）
docker run --rm \
    -v nginx_certs:/etc/letsencrypt \
    -v "$(pwd)/static:/var/www/certbot" \
    -p 80:80 \
    certbot/certbot certonly \
    --webroot -w /var/www/certbot \
    -d "$DOMAIN" \
    --email your-email@example.com \
    --agree-tos --no-eff-email

echo "==> 证书已签发，配置 Nginx 443..."

# 2. 生成 nginx ssl 配置片段（追加到 nginx.conf 的 server 块前）
cat >> nginx-ssl.conf <<EOF
server {
    listen 443 ssl;
    server_name $DOMAIN;
    ssl_certificate     /etc/letsencrypt/live/$DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$DOMAIN/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    include /etc/nginx/gateway-routes.conf;
}

# HTTP 全部跳转 HTTPS
server {
    listen 80;
    server_name $DOMAIN;
    return 301 https://\$host\$request_uri;
}
EOF

echo "==> 生成的 nginx-ssl.conf 需挂载进 nginx_lb 容器并 reload"
echo "==> 注意: 请将域名与邮箱填入脚本顶部常量，并重启 nginx 容器"

# 3. 注册自动续期（每月 1/15 号）
(crontab -l 2>/dev/null | grep -v "setup_ssl.sh --renew"; \
 echo "0 3 1,15 * * bash $(pwd)/setup_ssl.sh --renew >> /var/log/ssl-renew.log 2>&1") | crontab -
echo "==> 已注册自动续期 cron"
