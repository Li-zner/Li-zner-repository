# Agent 网关 — 灾难恢复手册

> 版本: v1.0 / 2026-08-02
> 目标: 回答"数据库/容器挂了怎么办"，并规定定期演练。

## 一、备份现状

- 备份目录: `backups/`（`gateway_db_YYYYMMDD_HHMMSS.sql`，pg_dump 文本格式）
- 备份脚本: `scripts/backup_db.sh`（建议每天 cron 执行）
- 其他持久数据: `.env`（`/etc/agent_gateway/.env`）、`static/`（代码库已备份）

## 二、恢复演练（每季度必做）

```bash
bash scripts/restore_drill.sh          # 恢复到临时库并校验行数
```

通过标准:
- 6+ 张核心表行数与生产库一致
- 无 psql 错误输出
- 演练结果记录在本文档底部

## 三、真实故障恢复流程

### 场景 A: 单个网关容器挂了
```bash
# Docker HEALTHCHECK 会自动重启；或手动
docker compose up -d --force-recreate gateway
```

### 场景 B: 全部容器挂（机器重启）
```bash
bash start_all.sh        # 一键拉起全部服务（含 nginx/prometheus/grafana）
# 验证
curl -s http://localhost:10090/health
```

### 场景 C: 数据库数据损坏/误删
```bash
# 1. 停止网关写入
docker compose stop gateway gateway2 gateway3 gateway4

# 2. 恢复备份到正式库
#    先备份当前损坏库（保留现场）
docker exec postgres pg_dump -U agent_user -d agent_gateway > backups/gateway_db_crash_$(date +%Y%m%d_%H%M%S).sql
#    删除并重建正式库
docker exec postgres psql -U agent_user -d postgres -c "DROP DATABASE agent_gateway"
docker exec postgres psql -U agent_user -d postgres -c "CREATE DATABASE agent_gateway"
#    恢复
docker cp backups/gateway_db_最新.sql postgres:/tmp/restore.sql
docker exec postgres psql -U agent_user -d agent_gateway -q -f /tmp/restore.sql

# 3. 重新拉起网关
docker compose up -d gateway gateway2 gateway3 gateway4

# 4. 验证数据完整（行数对比）
```

### 场景 D: `.env` 丢失
```bash
# 备份位置: /etc/agent_gateway/.env (WSL) 或项目根 .env
# 从 Git 提交的 .env.example 重建，填入密钥（DEEPSEEK/AMAP/GITHUB/SMS/JWT/SESSION）
```

## 四、RPO / RTO

- **RPO（数据丢失容忍）**: 取决于备份频率。每天备份 = 最多丢 1 天数据
- **RTO（恢复时间目标）**: 目标 < 30 分钟（不含下载依赖）

## 五、演练记录

| 日期 | 备份文件 | 结果 | 备注 |
|------|---------|------|------|
|      |         |      |      |

---
*约定: 每次演练后更新上表；备份脚本变更需同步更新本手册。*
