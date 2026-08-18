---
name: 密钥与配置治理
description: "执行配置与密钥治理：唯一权威源、密钥从代码剥离、防配置漂移、轮换流程。当任务涉及'密钥/配置/环境变量/.env/安全目录/轮换'时使用。"
---

# 密钥与配置治理

原则：**配置是代码与环境的交界**。同一份代码，不同环境不同配置；密钥一旦进 git 历史就
永久泄露。以下流程来自 agent_gateway 真实事故复盘（密码漂移导致线上全链路故障）。

## 一、权威源设计（防漂移）

- **唯一权威源**：安全目录一份完整 .env（如 WSL `/etc/<project>/.env`，root 所有）
- 根目录 `.env` 只留占位注释，不存放任何密钥
- docker-compose `env_file` 直接指 UNC 路径（`\\wsl.localhost\Ubuntu\etc\<project>\.env`，
  正反斜杠都行——compose CLI 在 Windows 侧运行，支持 UNC）
- `_env.py`（零依赖读取器）同时读：进程环境变量 → 根 .env → 安全目录（先读到的优先）
- **权限坑**：安全目录文件必须 **644**（compose/docker CLI/宿主脚本以普通用户经 UNC 读取，
  600 会全部 PermissionError）；单用户 WSL 可接受，多用户应改用 Docker Secrets

## 二、密钥从代码剥离（硬约束）

- `os.getenv()` 读取，**config.py 移除默认值**（缺失即启动失败 = fail loudly）
- 提交前扫描：`grep -rn "sk-\|password\|secret" --include="*.py"`；git 历史里的密钥
  用 `git filter-repo` 清洗（替换为 REDACTED_ 占位）
- `.gitignore` / `.dockerignore` 排除 .env、备份、日志、本地产物（注意 .gitignore 必须是
  UTF-8——GBK 乱码会让规则静默失效，简历/密钥文件漏忽略）

## 三、防配置漂移（真实事故复盘）

事故：容器内密码被外部改成 X，git 配置仍是旧密码 → 4 网关连库失败 → 登录 500 → 支付全链路挂。

1. 密码统一收口 `.env`（gitignored）+ `env_file` 注入，禁止散落硬编码
2. 排查命令：`docker exec <容器> env` 看真实环境变量 ≠ 配置文件 → 锁定漂移
3. **改密码后必须全链路探活**（nginx→login→钱包→下单→支付）
4. 部署脚本加配置一致性校验；禁止「手改容器」
5. 环境变量注入方式必须端到端验证：`docker compose config` 看实际值（env_file + ${VAR} 曾取空值）

## 四、密钥轮换流程（双 Key 并行期）

以 JWT 为例（脚本模式见 agent_gateway `scripts/rotate_jwt_secret.sh`）：

1. 新密钥写入安全目录，**旧值保留为 `<KEY>_OLD`**（追加前检查 env 文件尾部换行！）
2. 重建容器 → 并行期旧密钥仍可验（解码时两个密钥都试）
3. 旧 token 全过期后移除旧密钥 → 再重建
4. 验证：旧 token 200 / 新签发 200

## 五、编码与文件卫生（配置相关）

- .env / ini 文件：**UTF-8 无 BOM**（PowerShell 写 UTF8 会带 BOM，读用 `utf-8-sig` 兼容）
- alembic.ini 等 ini：**保持 ASCII**（Windows configparser 用 GBK 读，中文注释直接炸）
- env 文件**尾部必须有换行**：`echo "K=V" >> file` 前检查 `tail -c 1`，无 `\n` 先补空行，
  否则新行拼到上一行、密钥互相污染
- bash 脚本 LF 换行（`.gitattributes` 强制 `*.sh eol=lf`）

## 六、验收清单

- [ ] 代码中无硬编码密钥（grep 扫描通过）；config 无默认密钥值
- [ ] 唯一权威源已建立，根 .env 已清空为占位
- [ ] `docker compose config` 确认 environment 来自安全目录
- [ ] 权限 644；密钥不出代码仓库边界
- [ ] 轮换脚本有 dry-run 与验证步骤
- [ ] 改任何密钥后完成全链路探活
