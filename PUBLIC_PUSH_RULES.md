# public 推送规则（求职/作品集用途）

> 本仓库（agent_gateway_public → Li-zner-repository）用于求职展示。
> 同步来源：本机 agent_gateway（重构后的最新代码）。
> 原则：**只推核心、能体现工程能力的代码与资产；绝不泄露密钥/数据/环境配置**；杂文件不推。

## 一、推（include）
- **核心应用代码**：`app/`（含重构后的 services 层，是本次亮点）
- **数据库迁移**：`alembic/`、`alembic.ini`
- **部署/运维**：`Dockerfile`、`docker-compose.yml`、`start_all.sh`、`ci.sh`、`deploy.sh`、`nginx.conf`、`requirements*.txt`、`pytest.ini`、`_env.py`
- **工程文档**：`docs/`、`README.md`、`AGENTS.md`、`app/core/agent_rules.md`
- **代码审查/复盘**：`code review/`
- **可复用资产**：`可复用代码/`、`可复用资产/`
- **作品集**：`showcase/`、`mcp_assets/`、`prompts/`、`static/`
- **测试**：`tests/unit/`
- **文档类**：`.docx`、`.md`（`docs`、`code review` 里的）
- **基础配置**：`.env.example`、`.gitignore`、`.gitattributes`、`.dockerignore`、`ci.sh`

## 二、不推（exclude）
- **密钥/配置**：`.env`、`.env.*`（除 `.env.example`）、`etc/`、`login*.json`、`*.pem`、`*.key`、`secrets*.json`、`*_secret*.json`、`*_credentials*.json`
- **数据/备份**：`backups/`、`*.sql`、`*.sql.gz`、`*.dump`、`.tmp/`、`_backups/`、`uploads/`、`cdc_journal/`、`*.log`、`*.tar.gz`
- **本地产物**：`__pycache__/`、`.venv/`、`.pytest_cache/`、`.pyc`、`node_modules/`
- **编辑器/临时**：`.idea/`、`.vscode/`、`.cursor/`、`.workspace`
- **杂/本地私有**：`_ref/`、`exports/`、`langgraph_lab/`、`agent_gateway_backup/`、`deploy/*.local`、`gitpush_daily.cmd`、`agent_gateway_public/`、`dc_out.py`、`*.bak`
- **平台/依赖缓存**：`node_modules`、`_next`、`dist`

## 三、同步方式
- 用 `rsync` 按上面 include/exclude 从 `agent_gateway` → `agent_gateway_public`。
- **`--delete` 删除 public 中不在转出清单的旧文件**（真正“删旧换新”），但**保留 `showcase`、`README` 等作品集内容**（它们是 public 特有，不会被误删）。
- 同步后 `git add -A` + commit，再推 `origin`(Li-zner-repository)。

## 四、推送
- **私人仓库**：`origin`(agent-gateway-full) —— **全量推送**（所有提交）。
- **公开仓库**：`agent_gateway_public` → `origin`(Li-zner-repository) —— **按本规则**（核心+资产，勿泄密）。
