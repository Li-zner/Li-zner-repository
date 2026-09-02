---
title: agent_gateway 代码笔记
created: 2026-07-16
tags:
  - project/gateway
  - coding-notes
  - seedling
aliases:
  - 网关笔记
---

# agent_gateway 代码笔记

> 项目开发过程、修改记录、设计决策记录。
> 相关：[[陷阱与模糊点]] · [[学习计划-手写编程训练]] · `docs/` 学习体系（工程全链路总结 / 生产级差距与核心机制手册 / 面试复习宝典）

## 项目简介

基于 FastAPI + DeepSeek 的自研 Multi-Agent 网关。对等协作架构，各 Agent 领域垂直独立，语义路由自动分发任务。已上线公网，具备服务治理、可观测性与安全合规体系。

## 修改记录

| 日期 | 内容 | 备注 |
|------|------|------|
| 2026-07-24 | 模型切换、温度统一 0.3、4 实例部署 | config.py + docker-compose |
| 2026-07-24 | 数据库连接池 + 用户隔离限流 | db.py + rate_limit.py |
| 2026-07-24 | 移除 Dify 降级 + 压测修复 | v1.py 废弃, API Key 注入修复 |
| 2026-07-28 | **新增求职助手人格 me.json** | 独立端点 /v2/chat/me，纯 LLM 无工具路由 |
| 2026-07-28 | **前端模式切换（旅行转求职）** | 单页面切换，蓝框徽标，独立历史 |
| 2026-07-28 | 人格方位图 + Prompt 精简 + 夸大限制 | 务实规则，薪资 10-12K 引导详谈 |
| 2026-08-04 | **start_all.sh 一键启动脚本重写** | 默认跳过构建（秒起）、`--build` 强制重建、幂等 `up -d`（不再四实例同时 force-recreate 停摆）、启动后健康检查等待、完整状态表 |
| 2026-08-04 | **start_all.sh 默认单实例模式** | 默认仅拉起 gateway(10092)，自动停掉其余 3 实例；`--all` 才拉起全部 4 实例；nginx 用 `--no-deps` 启动（否则 depends_on 会把停掉的实例带起来） |
| 2026-08-04 | **start_all.sh 集成自动伸缩** | 启动脚本时自动拉起 autoscale.sh 调节器（`--no-autoscale` 跳过）；第 3 步基础设施改为显式列出服务，避免无参数 `up -d` 把停掉的网关拉起来 |
| 2026-08-04 | **Prometheus 采集目标修复** | 旧目标 `gw_10092:10086`（容器不存在，`no such host`）导致网关无任何指标 → 改为 `agent_gateway*:10086` 四个真实容器 |
| 2026-08-04 | **新增自动伸缩调节器 autoscale.sh** | 从 Prometheus 查网关总 QPS，连续高于高阈值自动扩 gateway2→3→4，连续低于低阈值自动缩（至少留 min）；阈值/间隔/冷却全可调；实测 1→3→1 端到端验证通过 |
| 2026-08-05 | **CDC 变更数据捕获（方案A：触发器+事件表+Worker）** | 捕获 payment_orders/transaction_logs/user_wallets 的增删改 → cdc_events 表 → JSONL 落盘 cdc_journal/（SSD，挂载卷）；Redis 锁单写者；checkpoint 断点续传；消费 API；对账验收 1000 条 100% 一致 |
| 2026-08-05 | **修复 users 表 schema 缺失列** | 旧库 users 表只有 4 列（缺 phone/display_name/email/avatar_url/extra/updated_at），导致 /api/user/profile 500 → ALTER TABLE 补列 |
| 2026-08-05 | **单网关性能压测** | /health 达 356.7 QPS（并发50）、312.6 QPS（并发100）≥300 目标 ✅；profile（JWT+DB）~250-280 QPS 零错误；并发 200 后 asyncio 单 worker 排队吞吐下降 |
| 2026-08-04 | **新增 .gitattributes** | `core.autocrlf=true` 会把 .sh 检出成 CRLF 破坏 bash 脚本 → 强制 `*.sh eol=lf` |

## 求职助手人格设计规则

### 定位
第三人称人格「黎忠南」，求职场景代表用户回答访客。与旅行/民法典系统完全断开——独立端点 /v2/chat/me、独立模型 DeepSeek Flash、无路由无工具。

### 人格方位图

| 维度 | 设定 |
|------|------|
| 沟通长度 | 适中，说清楚就好 |
| 正式度 | 偏随意，像朋友聊天 |
| Emoji/语气词 | 几乎不用 |
| 中英混杂 | 纯中文（技术名词除外） |
| 社交风格 | 礼貌客气，不自来熟 |
| 被质疑 | 摆事实讲数据，不争辩 |
| 不知道的事 | 先坦诚不清楚，再说自己思考 |
| 谦虚度 | 低调谦虚，不夸大 |

### 回答规则
- 用「我」自称，不是专家，是23岁应届生
- 不知道就说不知道，不装资深
- 薪资报10-12K范围，具体引导找本人详谈
- 不熟悉的技术坦诚接触不深，不以导师口吻给建议
- 不夸大项目规模和成本数字，不代替用户承诺

## 压测记录 (2026-07-24)

首次压测 200 用户发现的主要问题：

| 问题 | 根因 | 修复 |
|------|------|------|
| V2 HTTP 500 | API Key 在 Windows Docker 取空值 | env_file 直接注入 |
| 流式未完结 | Key 缺失时 raise 导致 500 | 友好提示 + 补发结束标记 |
| V1 100% 失败 | Dify 未配置 | 已移除 Dify |
| 登录偶发 502 | 容器启动未就绪 | 环境变量补全 |
| 限流太严 | 并发上限 20 | 放开到 200 |

教训: Locust 必须设 --run-time，避免无限制跑消耗 token。

## 架构速览

`
nginx:10090 --- agent_gateway(10092), agent_gateway2(10089), agent_gateway3(10093), agent_gateway4(10094)
`

### 目录
- app/main.py - 应用入口
- app/routes/v2.py - DeepSeek 主力路由 + 求职助手独立端点
- app/core/config.py - 配置管理
- app/agents/ - Agent 逻辑（路由/编排/工具）
- app/middleware/ - JWT 认证 / 限流 / 熔断
- prompts/ - 系统提示词（unified / travel / civil_code / me）
- static/ - 前端页面（index.html / map.html）

---

## 工程化建设 (2026-08-02)

### 1. 版本控制 + 密钥清洗
- GitHub 私有仓库: `https://github.com/Li-zner/Li-zner-repository`（origin/main）
- 基线提交: `79a36f5`（255 文件）；CI/CD 提交: `f2a1cf9`
- 新建根目录 `_env.py`：零依赖 .env 读取器，`get(key)` / `db_url(host, db)`，密码自动 URL 编码
- **41 个文件**硬编码密钥已清除（DeepSeek/AMAP Key、数据库密码、admin 密码）→ 全部改从 `.env` 读取
- `config.py` 的 POSTGRES_DSN 不再含密码默认值（fail loudly）
- ⚠️ 建议：DeepSeek API Key 曾散落代码与对话中，应去控制台轮换

### 2. CI/CD 流水线（ci.sh）
- 五阶段: [1] AST 语法检查 → [2] docker build(层缓存+commitSHA标签) → [3] 滚动部署(逐个实例) → [4] nginx 冒烟 → [5] 完成；失败自动回滚
- `deploy.sh` 已移除 `--no-cache`（曾导致全量重装 torch 极慢）
- 用法: `bash ci.sh --no-deploy` / `bash ci.sh` / `bash ci.sh --rollback <镜像ID>`

### 3. 数据库迁移（Alembic）
- `alembic/` async 模板 + `scripts/migrate.py` 迁移助手
- 基线迁移 `76a1f09d2ac8`：13 张表 + pg_trgm/vector 扩展 + 索引（op.execute 忠实还原 DDL）
- 已在临时库 `agent_gateway_test` 完整验证（14 表含 alembic_version）
- 用法: `python scripts/migrate.py upgrade head / stamp head / revision -m "..."`

### 4. 环境大坑：wslrelay 拦截 5432
- `localhost:5432` 被 `wslrelay.exe`(127.0.0.1) 拦截 → 认证失败（与 10091 端口同类问题）
- postgres 容器 IP 从 Windows 宿主不可达（WSL2 桥接限制）
- **可靠路径**: 局域网 IP（命中 `com.docker.backend` 的 0.0.0.0:5432）
- `scripts/migrate.py` 自动探测: DB_HOST > localhost(可用) > 局域网 IP
- 坑: alembic configparser 的 `%` 插值遇密码 `%23` 报错 → 不写 ini，直接向引擎传 URL

### 5. 安全与运维加固（文件已产出，待部署）
- `nginx.conf`: 简易 WAF（SQLi/XSS/路径穿越 → 403）、安全头、限流盾牌、连接限制
- `alert.rules.yml`: 9 条 SLO 告警（错误率/流量骤降/p95/LLM token 突增/缓存命中率/支付失败率/下游/实例）
- `docs/SLO.md`: SLI/SLO 定义 + 容量基线（800 并发实测）
- `scripts/retention_cleanup.py`: 数据保留清理（dry-run 支持）
- `scripts/setup_ssl.sh`: HTTPS 证书自动化（certbot + 自动续期）
- `scripts/scale_gateway.sh`: 运行时加/减网关实例
- `scripts/restore_drill.sh` + `docs/disaster-recovery.md`: 备份恢复演练

### 6. 测评数据（用户提供）
- `tests/test_cases.yaml`: 112 条用例（旅行 40 + 鲁棒性 6 + 攻击 6 + 民法典 60）
- `civil_evaluation_report.xlsx`: 60 条平均 8.95/10
- 800 并发压测近零错误（用户实测）

---

## 会话记录追加（2026-08-02 ~ 08-10）

### 修改记录（续）
| 日期 | 内容 | 备注 |
|------|------|------|
| 08-02 | Git 版本控制 + 密钥清洗（41 文件） | `_env.py` 统一读 .env |
| 08-02 | CI/CD 流水线 ci.sh | 滚动发布 + 自动回滚 |
| 08-03 | Alembic 数据库迁移 | 基线 13 表，临时库验证后 stamp |
| 08-03 | 生产化工程体系 | WAF / SLO 告警 / 保留 / 伸缩 / 灾备 / HTTPS 脚本 |
| 08-10 | 支付系统并发安全修复 | 锁持有者校验 + 乐观锁重试 + 扣费冲突检查 |
| 08-10 | 简历 + 展示目录 | `showcase/` HR 视角 + `黎忠南简历-AI后端开发.docx` |
| 08-10 | docs 学习体系 | 5 份核心文档（见下） |
| 08-11 | **仓库重组：公开核心 + 私有完整双仓库** | 删库重建，见下方章节 |
| 08-12 | **VS Code 终端异常根治** | 僵尸会话清 4 个（最早挂 4 天）；默认终端 CMD→PowerShell；新增 `scripts/fix_terminal.ps1` 一键自愈（清 24h 僵尸 + PATH 自检，纯 ASCII 防 GBK 解析坏）；**一劳永逸**：`terminal.integrated.env.windows` 强制注入 PATH（git/docker/gh/cloudflared 追加），计划任务 `agent_gateway_terminal_cleanup` 每日 04:30 自动清僵尸 |
| 08-12 | **求职助手人设更新** | `prompts/me.json` 重写：严格锚定简历 PDF（技术栈 熟练/熟悉/了解 三级边界、项目关键数字固化 800并发/1600并发99.28%/0.925、防显摆规则——简历没写的不拓展、被问起坦承接触不深）；薪资/联系方式按用户要求保留旧值（10-12K、含 QQ，与简历 8-11K 不一致以用户为准）；**坑**：`_load_me_prompt()` 用 `encoding="utf-8"` 读 me.json，文件带 BOM 会 JSONDecodeError（编辑工具易引入 BOM）→ me.json 必须无 BOM；改后需 `docker build` + `--force-recreate gateway` 生效（prompts/ 未挂载卷） |

### 当前进度与接续点

> [!warning] 推送注意
> 本环境持久终端会偶发损坏（git 不在 PATH），推送输出易被吞。
> 手动命令：`cd d:\桌面\agent_gateway && git push origin main`
> 凭据在 Windows 凭据管理器已有，勿覆盖。

> [!info] 环境大坑速查
> `localhost:5432` 被 wslrelay 拦截 → 用局域网 IP（`scripts/migrate.py` 自动探测）。
> WSL python 不能写 Windows 盘 pyc → 语法检查用 AST 解析。
> `alembic.ini` 必须 ASCII（GBK 会炸中文注释）。
> `config.py` 已移除 DB 密码默认值（环境变量注入）。

### 待办
- 推送确认（最后一次提交可能未推上）
- 配置迁移待生效：下次重建容器时环境变量迁移才生效（勿强制热重启）
- 本批改动（`.env` / `docker-compose.yml` / `start_all.sh` / wiki）提交并推送
- ~~生产补缺 8 项~~ → **2026-08-11 全部完成**（见下方章节）
- **运维收尾（2026-08-11 半小时包）已完成**：ExceptionRateHigh 告警（promtool 10 规则 SUCCESS）、
  备份计划任务 `agent_gateway_backup`（每日 03:00）、慢查询自启（启动文件夹 .bat）、CI [1.6] 集成测试可选步骤
- **手写训练计划从第 1 天开始** → [[学习计划-手写编程训练]]

### 本轮交付的 docs 学习体系（2026-08-10 重构为 6 份）
- [[docs/项目速查卡|项目速查卡]] — 代码地图 + 主链路 + 数字速查（面试/日常只看这份）
- [[docs/工程全链路总结|工程全链路总结]] — 项目全景（旅程 + 16 概念 + 踩坑）
- [[docs/生产级差距与核心机制手册|核心机制手册]] — 24 机制 + 7 模板（含 JWT/锁/幂等）
- [[docs/面试复习宝典|面试复习宝典]] — 100 题 + RAG/LLM + 运维 + 转行话术
- [[docs/运维手册|运维手册]] — SLO + 容量 + 灾备（原 SLO/灾备 合并）
- [[docs/payment-system-architecture|支付系统架构]] — 支付深挖
- 归档：`docs/archive/`（SLO / disaster-recovery / 分布式锁笔记 原稿）

### 线上故障复盘 + 配置治理 (2026-08-11)

> [!danger] 2026-08 线上故障：postgres 密码漂移
> 容器内密码被外部改为 `agent_pass`，与 git 配置 `pg_***`（已脱敏）不一致，
> 导致 4 网关连库失败 → 登录 500 → 支付全链路挂。
> 修复：`ALTER USER agent_user WITH PASSWORD '...'` + 重启网关 + 全链路探活通过。
> 教训：配置必须集中管理，改动必须走 git + 部署脚本，禁止"手改容器"。
> 详见 [[陷阱与模糊点#17]]。

#### 配置治理：权威源迁移到容器安全目录（最终架构）

> [!info] 唯一权威源 = `/etc/agent_gateway/.env`（WSL，root 所有/写，644 可读）
> 桌面快捷方式「Agent网关配置.env.lnk」→ `scripts/edit_env.vbs` → 管理员记事本打开。
> 改完配置后需重建容器生效。权限用 644 而非 600：compose/docker/宿主脚本以普通用户
> 经 UNC 读取，600 会全部 PermissionError。详见 [[陷阱与模糊点#19]]。

- 合并根 .env 与旧安全目录 .env（root 优先 + 保留扩展键 DIFY/GITHUB/SMS/EMBEDDING 等）
  为完整版，写入 `/etc/agent_gateway/.env`（29 键，备份 `.bak-20260810`）。
- `docker-compose.yml` 的 5 处 env_file 改指 `\\wsl.localhost\Ubuntu\etc\agent_gateway\.env`
  （compose 可直接读 UNC，已验证）。
- `_env.py` 增加 UNC 读取路径（Windows 宿主机脚本才能读到安全目录）。
- `scripts/scale_gateway.sh` 的 `--env-file` 改指安全目录，并移除硬编码 `DATABASE_URL`。
- 根目录 `.env` 已清空为占位注释，不再存放任何密钥。
- 宿主机脚本链路：`_env.py`（根 .env → 安全目录，先读到的优先）→ `db_url()`/`get()`。
- 验证：`docker compose config` 确认 environment 来自安全目录；**下次重建容器时生效**（不强制热重启）。

#### 单实例 + Nginx 自动伸缩 (2026-08-10)

> [!important] nginx upstream 必须与运行实例同步
> Docker DNS 不解析已停服务，nginx reload 引用已停实例会报 host not found。
> 详见 [[陷阱与模糊点#21]]。

- 运行态：`gateway(10092)` 1 实例 + `nginx_lb`，其余由 `autoscale.sh` 按 QPS 扩缩（高>40/低<12，min=1 max=4）。
- `autoscale.sh` 新增：扩容时把实例加入 nginx upstream + reload；缩容时先摘除再停；
  `reconcile` 子命令幂等对齐 upstream 与运行实例。
- `nginx.conf`：upstream 由 autoscaler 维护（勿手改）；三个 location 均加
  `proxy_connect_timeout 2s` + `proxy_next_upstream_tries 4`（防死实例拖垮请求）。
- `start_all.sh`：nginx 启动后自动 `reconcile`，避免切换实例模式后 upstream 残留。
- 本次操作：停 gateway2/3/4 → 重建 gateway（新 env_file 从安全目录生效，容器出现 DIFY_API_URL 确认）
  → 修复 nginx 登录超时（upstream 残留 + 认证 location 无重试）→ 重启 autoscaler。

### 生产补缺 8 项完成 (2026-08-11)

> 全部落地并验证，已随镜像重建部署到运行中的 gateway。

1. **pytest 套件 + 接入 CI**：`tests/unit/`（25 个离线单测：DFA/LRU/分布式锁/乐观锁/重建锁），
   `tests/integration/`（支付幂等）；`pytest.ini` + `requirements-dev.txt`；ci.sh 新增 [1.5] 步骤。
2. **超时收口**：config.py 定义 HTTP_TIMEOUT_SHORT/MEDIUM/LONG、MAP_API_TIMEOUT、
   DB_ACQUIRE_TIMEOUT、TASK_TIMEOUT、LLM_CONNECT_TIMEOUT；替换全部散落硬编码 timeout。
3. **慢查询监控**：postgres 加载 `pg_stat_statements`（compose command + 重建，数据无损），
   `scripts/slow_query_monitor.py`（--check/--loop）+ `enable_pg_stat_statements.sh`。
4. **缓存防击穿/雪崩**：`semantic_cache.py` 增加互斥重建锁（Redis SET NX + Lua 释放）+ L0 随机过期；
   v2.py 流式路径接入（锁获取失败降级重建，finally 释放）。
5. **备份恢复演练真实执行**：backup_db.sh 统一到项目 `backups/gateway_db_*.sql`；
   restore_drill.sh 实测通过——7 张核心表行数与生产一致。
6. **支付幂等测试**：`tests/integration/test_payment_idempotency.py` 实测通过
   （重复支付被拒、余额不变、流水仅 1 条）。
7. **自建异常聚合**：`core/error_aggregator.py`（Redis 计数+样本 + Prometheus app_exceptions_total），
   monitor_requests 中间件接入，`/api/admin/errors` 管理端接口。
8. **JWT 密钥轮换**：config 增加 JWT_SECRET_OLD；auth.py 双密钥解码（并行期旧 token 仍有效）；
   `scripts/rotate_jwt_secret.sh`（真实轮换已执行并验证：旧 token 200 / 新登录 200）。

**新踩的坑**：合并/追加 env 文件时若文件尾部无换行，新行会拼到上一行尾部损坏配置
（POSTGRES_PASSWORD 与 JWT_SECRET_OLD 被拼接）。已修 rotate 脚本加尾部换行保护，见 [[陷阱与模糊点#22]]。

---

## 仓库重组：公开核心 + 私有完整（2026-08-11 完成）

> 背景：简历链接 `github.com/Li-zner/Li-zner-repository` 需指向可公开内容，
> 但担心完整生产代码公开 → 拆分为「公开核心展示」+「私有完整代码」双仓库。

### 双仓库架构（最终形态）

| 仓库 | 可见性 | 内容 | 用途 |
|------|--------|------|------|
| `Li-zner/Li-zner-repository` | 🔓 PUBLIC | README + showcase/（核心代码脱敏快照）+ 简历 PDF | 简历/作品集入口，招聘方查看 |
| `Li-zner/agent-gateway-full` | 🔒 PRIVATE | 完整生产代码（含支付/脚本/测试/全部历史） | 完整代码备份，日常 push 目标 |

- 本地完整库 `d:\桌面\agent_gateway` 的 origin → 已改指 `agent-gateway-full`（**禁止误推上公开仓库**）
- 独立公开库 `d:\桌面\agent_gateway_public`（全新 git 历史，仅 2 提交）
  - README.md（项目首页）+ showcase/（核心展示）+ `黎忠南简历.pdf`
  - 推送：`cd d:\桌面\agent_gateway_public && git push origin main`

### showcase/ 核心展示目录

- `showcase/README.md` — HR/招聘方视角的项目展示（亮点/工程能力/关键数据/技术栈/演示）
- `showcase/core/` — 核心代码脱敏快照（27 个 .py）：
  - `agents/`（11）：自研 Agent 引擎——意图路由/执行器/多 Agent 编排/工具集/RAG
  - `core/`（13）：三级语义缓存/冷热记忆/人格管理/安全过滤/连接池
  - `middleware/`（3）：JWT 认证（令牌轮换）/分布式限流/熔断
  - `core/README.md` — 代码导读（推荐阅读顺序）
- 脱敏原则：密钥全部 `os.getenv()`，无硬编码凭据；不含支付/迁移/运维

### 关键操作记录

1. 历史清理：`git filter-repo` 替换历史中 DB 密码 `pg_Aq#7xLmZ@2wRt9`（明文+URL编码）为 `REDACTED_PASSWORD`，
   并排除全部简历文件历史；备份镜像 `backups/repo_mirror_before_rewrite.git`
2. `.gitignore` 末尾两行曾是 GBK 乱码 → 规则失效（简历 docx 未被忽略）→ 已修复为 UTF-8
3. 安装 `gh` CLI（v2.97.0，winget）并授权（需 `delete_repo` scope）
4. 删库重建流程：创建私有备份 `agent-gateway-full` 并推完整代码 → 删 `Li-zner-repository`
   → 重建同名 PUBLIC → 推送 showcase-only 独立库 → 本地 origin 改指私有备份
5. 简历最终公开版 = `黎忠南简历.pdf`（2026-08-11 用户新做）

### 踩坑备忘

- 本机到 GitHub 443 极不稳定 → push 失败重试（间隔 5-8s，最多 5 次），gh 操作同理
- gh 删库需要 `delete_repo` scope：`gh auth refresh -h github.com -s delete_repo`
- 新建独立 git 库默认分支是 `master` → `git branch -m main` 后再 push
- gh auth login 设备码流程：终端显示 one-time code → 浏览器 `github.com/login/device` 输入
