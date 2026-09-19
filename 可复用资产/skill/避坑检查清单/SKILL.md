---
name: 避坑检查清单
description: "按 8 类 43 条真实踩坑记录逐条对照（Windows/Docker/编码/配置/数据库/异步/AI检索/MCP/部署），写代码或排障前过一遍，避免重复犯错。当任务涉及'排障/环境问题/Windows+Docker/中文编码/上线部署'时使用。"
---

# 避坑检查清单

每条 = 坑 → 根因 → 解法。来自 agent_gateway 全项目踩坑合并（docs/踩坑与陷阱总汇.md 43 条）。

## 一、环境与工具链（Windows / Docker / 终端）

1. **Docker 构建太慢**：`--no-cache` 全量重装依赖（torch 20 分钟）→ 依赖层在 COPY 代码之前，小改动 `docker cp` + `docker restart`
2. **taskkill docker.exe 后 CLI 不可用** → 重启 Docker Desktop（`Start-Service com.docker.service`）
3. **WSL python 写 Windows 盘 pyc 权限失败** → 语法检查用 `ast.parse` 纯解析，不写文件
4. **wslrelay 拦截 localhost:5432** → 宿主连 PG 用局域网 IP（命中 com.docker.backend 的 0.0.0.0:5432）；连接探测顺序 DB_HOST > localhost > 局域网 IP
5. **持久终端偶发损坏（^U 前缀/git 找不到）** → PSReadLine 2.0.0 与 VS Code OSC 序列冲突；升级 2.4.5（新开终端生效），出现 ^U 换终端，push 输出 `*> file` 确认
6. **中文路径 venv ensurepip 失败** → 避免中文路径建 venv，或用已有解释器
7. **pip 安装被沙箱拒绝** → 完整权限执行；装包属一次性操作

## 二、编码与字符集

8. **UTF-8 BOM 导致 json.load() 失败** → 读用 `utf-8-sig`，写用 `UTF8NoBOM`（PowerShell `Set-Content -Encoding UTF8` 会写 BOM！）
9. **PowerShell 管道传中文乱码（GBK）** → 中文代码写 .py 文件 docker cp 进容器执行
10. **控制台 GBK 乱码** → `PYTHONIOENCODING=utf-8`；程序里避免打印 emoji
11. **alembic.ini 中文注释炸（GBK）** → ini 文件保持 ASCII（纯英文注释）
12. **bash 脚本 CRLF 炸** → 脚本必须 LF（`.gitattributes` 强制 `*.sh eol=lf`）
13. **env 文件尾部无换行 → 追加拼行损坏配置** → 追加前检查尾字节补 `\n`，或 `printf '\n%s\n' "K=V" >> file`

## 三、配置与密钥治理

14. **密钥硬编码散落** → `_env.py` 统一读取；config.py 移除默认值（fail loudly）；.gitignore 排除
15. **configparser 遇密码 % 报错** → 连接串绕过 ini，直接传引擎
16. **双 env 文件分叉（配置漂移）** → 合并为一份完整版写入安全目录；compose env_file 指 UNC 路径；**权限 644**（600 会让普通用户 UNC 读取全 PermissionError）
17. **配置漂移导致线上全链路故障** → 排查看 `docker exec <容器> env` 真实环境变量；改密码必须全链路探活；部署脚本加一致性校验
18. **compose env_file 可直接读 UNC** → 密钥只存 WSL 安全目录不落项目文件夹（技巧）
19. **env_file + ${VAR} 取空值** → `docker compose config` 端到端验证实际注入值

## 四、数据库与 PostgreSQL

20. **asyncpg 时区不兼容** → offset-aware datetime 写不进 TIMESTAMP；用 `datetime.utcnow()` 或 isoformat 加 Z
21. **密码含 # @ 连接串炸** → `quote_plus` 编码；Shell 单引号包裹；不手动拼 DATABASE_URL
22. **慢查询拖垮连接池** → pg_stat_statements 监控 + 告警；`async with pool.acquire()` 保证归还
23. **重复导入（md5 幂等失效）** → 按 chunk_key 去重；清理保留带 embedding 的，其次内容长的

## 五、Python 与异步

24. **协程里阻塞调用** → `time.sleep`/同步 requests 卡死事件循环；IO 用 async，CPU 密集交线程/进程池
25. **异步落盘无保护** → `asyncio.create_task` 写库要信号量限流（如 5 并发）+ 异常兜底；生产用 MQ 替代进程内任务
26. **逐条 INSERT + 远程探测太慢** → 一次性探测 + executemany 批量插入 + 每批进度

## 六、AI / Agent 与检索

27. **模型级问题硬改 prompt 无效** → 架构手段绕过：映射表在 LLM 前拦截（CC019 2分→7分）
28. **正则漏导千位条号** → 条号正则字符类必须含「千」「零」；docx 全量重解析幂等补缺
29. **pg_trgm 对中文短查询失效 + 长句噪音** → 相似度全量排序（<0.1 丢弃）+ 子词 ILIKE 补充无条件执行
30. **docx 目录+正文双结构** → 以最后一次编标题为准（正文区）
31. **语义缓存 L0 清不掉** → 清 PG 表不清进程内 LRU；重启容器或等自然淘汰
32. **LLM 返回非法 JSON** → 多级提取（直接解析 → markdown 块 → 花括号 → 修复未引用 key）+ 自动重试一次

## 七、MCP 专项

33. **stdio 依赖命名管道，沙箱被拒（WinError 5）** → 完整权限终端运行（协议特性非代码问题）
34. **子进程 args 必须绝对路径** → 基于 `__file__` 拼路径
35. **Resource URI scheme 必须合法** → scheme 仅 `[a-zA-Z0-9+-.]`（下划线启动即崩）
36. **封装「能力」不是封装「流程」** → Tool=原子动作；编排逻辑留 Host
37. **参数暴露是取舍** → top_k 暴露、阈值收 Server 默认值
38. **依赖治理：自包含** → 不 import 宿主内部模块（会拉起整个应用初始化）

## 八、部署与运维

39. **滚动发布 vs 全量重建** → 一次只升级一个实例，健康再动下一个；失败回滚
40. **Docker DNS 不解析已停服务 → nginx reload 失败** → upstream 由 autoscale 动态维护；location 补 `proxy_connect_timeout 2s` + `proxy_next_upstream_tries 4`
41. **compose 与手动启动容器命名冲突** → 基础设施步骤容错（已运行则继续，失败仅告警）
42. **压测忘设 --run-time** → 压测命令显式限时（Locust 必须 --run-time）
43. **索引选型** → B-tree 范围/等值、GIN+trigram 模糊、ivfflat 向量近似；EXPLAIN ANALYZE 验证

## 使用方式

- **写代码前**：扫一眼相关类别（如涉及 Redis 看第五类，涉及中文看第二类）
- **排障时**：按症状定位类别 → 找「坑」→ 对照「根因」验证 → 按「解法」修复
- **新踩坑**：按主题追加到本清单对应类别
