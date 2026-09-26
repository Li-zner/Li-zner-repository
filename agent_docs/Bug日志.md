# Bug 日志——遇到的 bug 与修复方法（文案 + 代码）

> 2026-09-26 自仓库根迁入 `agent_docs/`；旧记录中的「Bug日志」路径均指本文件。

> 本文件是全项目 Bug 的聚集地。每条只写两件事：遇到了什么 bug（现象与根因）、怎么修复（方法与代码）。
> 来源一：副本漂移（模式级，2026-09-07 全仓审查提炼）。
> 来源二：2026-09-07 全仓审查第 0~8 批全部 bug。
> 来源三：docs/踩坑与陷阱总汇.md 的 48 条历史坑搬运。
> 维护约定：新坑追加；文档不限行数（600 行仅约束代码文件）。

---

# 第一部分：模式级 Bug——副本漂移（Copy Drift）

## 1.1 遇到的 bug

文案：同一段逻辑存在两份物理拷贝，修主份时副本不在视野里。这不是粗心，是结构必然——只要有第二份拷贝，漂移只是时间问题。隐蔽性在于：主库的测试全绿（修的是主库），副本带着旧 bug 继续服役，直到某个独立入口（MCP 演示、部署脚本）把它暴露给外人。

全仓审查实锤三例：

| # | 主实现（已修） | 副本（带病服役） | 后果 |
|---|---|---|---|
| 1 | `app/agents/tools.py` 港澳台"暂无天气数据"修复 | `mcp_assets/.../weather_server.py` 未同步 | 演示时 lives[0] IndexError，裸异常回传 Client |
| 2 | 发布脚本收口 `~/.zcode/cli/gw_publish.py` | `deploy/publish_lite.py` 旧副本带全部旧 bug | 误用旧版=静默发布旧镜像 |
| 3 | 主库检索演进为双路召回+RRF+本地重排 | `civil_code_server.py` 仍是旧三步流水线 | 对外演示能力落后主库一个版本 |

代码：案例 1 的漂移形态——

```python
# 主库 app/agents/tools.py（已修：缺字段防护）
live = weather_data["lives"][0]
if not live.get("weather") or not live.get("temperature"):
    return {"error": f"{city} 暂无天气数据"}

# 副本 weather_server.py（未同步，直接炸）
live = weather_data["lives"][0]          # lives 为空数组 → IndexError
return {"city": city, "temperature": f"{live['temperature']}℃", ...}
```

## 1.2 修复一：抽共享层（首选——能 import 就 import）

文案：单一事实源，副本瘦成声明薄壳。仓库内已有成功先例：资金原语拆 `_ledger.py` 后，充值/扣费/退款/延迟结算四条路径 import 同一份代码，乐观锁改行锁一处生效、资金路径从未漂移。

代码：weather_demo 的修法——它本来就 import 主库的 `_env.py`（并非真自包含），顺着把函数也 import 过来：

```python
# 修法：删掉手抄的 40 行高德调用，直接 import
from app.agents.tools import fetch_weather_async

@mcp.tool()
async def query_weather(city: str) -> dict:
    return await fetch_weather_async(city)
```

## 1.3 修复二：快照标注 + 自检（拷贝必须独立存在时）

文案：文件头写明快照身份（基线日期/主实现位置/已知差异），加一个比对自检把「静默漂移」变「构建期红灯」。

代码：两个副本文件头已落地标注（2026-09-07），自检如下：

```python
# tests/test_snapshots.py：标注缺席即红
def test_snapshot_annotations_exist():
    for snap, main, note in SNAPSHOTS:
        head = Path(snap).read_text(encoding="utf-8")[:1200]
        assert "【快照标注】" in head, f"{snap} 缺少快照标注（漂移防线失效）"
```

## 1.4 决策规则

| 场景 | 修法 |
|---|---|
| 副本能 import 主库 | 抽共享层 |
| 副本必须独立（资产外发/演示） | 快照标注 + 自检 |
| 副本已无价值 | 直接删除，留唯一权威版 |

---

# 第二部分：2026-09-07 全仓审查（第 0~8 批）

## 第 0 批：推送规则链路

### 0-1 镜像文件名错配 → 静默发布旧镜像

文案：文档写 `%TEMP%\gw_lite_xxx.tar.gz`（占位符），gw_publish.py 硬编码 `gw_lite_r4.tar.gz` 且忽略入参；存成新名字 → 上传旧包 → tag 相同容器不重建 → 健康检查照样过 → PUBLISH_DONE。
修复：显式入参 + 缺文件 loud fail + 容器镜像 ID 与刚 load 的 tag 一致性校验。

```python
LOCAL_TGZ = sys.argv[1] if len(sys.argv) > 1 else r"...\gw_lite_r4.tar.gz"
if not os.path.exists(LOCAL_TGZ):
    sys.exit(f"FATAL: 本地镜像不存在: {LOCAL_TGZ}")
```

### 0-2 健康检查 WARN 不阻断

文案：/health 60s 未通过只打 WARN，继续打印 PUBLISH_DONE、退出码 0——发布者把失败当成功。
修复：

```python
if not ok:
    sys.exit("FATAL: /health 60s 未通过,发布视为失败")
```

### 0-3 验证探针写死且不能证明"新"

文案：`ls place_extract.py`/`grep RECALL_LIMIT` 是某次快照，旧镜像同样有这些文件，验证形同虚设。
修复：替换为「容器镜像 ID == 刚 load 的 tag」一致性校验——旧包错发唯一能拦住的地方。

### 0-4 密钥扫描正则漏检（公开仓 P0 门禁破防）

文案：`GITHUB_CLIENT_SECRET=[A-Za-z0-9]` 漏引号变体；`sk-[A-Za-z0-9]{16,}` 漏 sk-proj；无 ghp_/LTAI/AKIA/JWT/连接串/通用赋值形状。
修复：新建 `scripts/secret_scan.py`（覆盖上述形状 + 字面量过滤防误报 + 只报文件:行号不回显密钥）+ `tests/test_secret_scan.py` 自检。

```python
GITHUB_CLIENT_SECRET="abcdef1234567890"   # 旧 grep 漏掉（= 后是引号），新扫描必抓
```

### 0-5 robocopy /MIR 空源清空目标

文案：源目录拼错/被移走时 /MIR 视为空源，删光公开仓对应目录。
修复：执行前前置守卫。

```bash
for d in "/d/桌面/agent_gateway/app" "/d/桌面/agent_gateway/frontend" "/d/桌面/可复用资产"; do
  [ -d "$d" ] || { echo "FATAL: 源目录不存在: $d"; exit 1; }
done
```

### 0-6 robocopy 绕过 .gitignore 带走未跟踪敏感文件

文案：git 不入库 ≠ 不被镜像；工作区临时落盘的 .env/.pem 会被照搬进公开仓。
修复：命令加 `/XF .env .env.* *.pem *.key *.p12 *.db *.sqlite3`（app 行另加 `/XD payment`）。

### 0-7 文档杂项（四处）

文案：第 19/69 行「见第四节」实为第三节；docker save 的 `%TEMP%` 在 Git Bash 不展开；「命中后处理」未写明必须改源目录（否则下次 /MIR 复活）；robocopy 退出码 0-7 皆成功无说明。
修复：全部在推送规则.md 内改写，docker save 一律 `cmd //c`。

## 第 1 批：app/main.py + core + middleware

### 1-1 会话历史并发回填竞态 → 上下文重复

文案：L1 缓存缺失时裸 pipeline 逐条 RPUSH 回填，同会话并发冷启动把同一批消息写两遍，LLM 上下文重复（trim 只裁条数不去重）。
修复：回填改原子操作（DEL+RPUSH 合一），并发只有一份。

### 1-2 fire-and-forget task 无引用可被 GC

文案：裸 `asyncio.create_task` 不持引用，事件循环只持弱引用，任务可被 GC 中途回收。散布 main.py 预热、memory_manager 双写/压缩、audit 写入、task_manager 幂等持久化。
修复：`core/concurrency.py` 新增 `spawn()` 统一持强引用 + done 回收，全部调用点替换。

```python
from ..core.concurrency import spawn
spawn(warmup_semantic_cache(), name="cache-warmup")
```

### 1-3 未处理异常原文（含 key 的 URL）入 Redis

文案：error_aggregator 存 `str(exc)[:200]`，httpx 异常带完整 URL（高德 key 在参数里），在 Redis 躺 7 天并展示于 /api/admin/errors。
修复：入库前过 `sanitize_error_text`（与任务状态/tool_result 出口同一消毒）。

### 1-4 file_ids 无上限

文案：每 id 2 次 Redis GET，无个数上限可放大往返。
修复：stream_utils 加 `MAX_FILE_IDS = 5` 兜底（schemas 源头约束另行补）。

### 1-5 get_redis 并发首调双初始化

文案：无锁双检，两协程同时见 None 会建两个连接池。
修复：`asyncio.Lock` 双检锁。

### 1-6 OTEL 端点硬编码

文案：`http://tempo:4318` 写死，lite 无 tempo 时后台静默重试。
修复：改环境变量注入。

### 1-7 APP_ENV 裸 os.getenv 双轨

文案：与 BaseSettings 双轨，同一配置两个来源。
修复：收编为 Settings 字段 `app_env`。

### 1-8 sms ttl_minutes 形参被忽略

文案：TemplateParam 硬编码 `"5"`，形参摆设。
修复：透传 `str(ttl_minutes)`。

## 第 2 批：app/routes

### 2-1 手机号登录防暴力锁键不一致

文案：检查锁用原始输入（手机号）、失败记账用解析后 username，键永远对不上 → 手机号登录可无限试密码。
修复：先 `resolve_login_username` 再检查与落锁，检查与记账同键。

### 2-2 默认密码 123456789 + 手机号可密码直登

文案：验证码自动注册设默认密码，而 /api/login 支持手机号+密码直登 → 知道手机号即可无短信登录所有未改密账号。
修复：注册改 `secrets.token_urlsafe(12)` 随机占位密码（不可登录），用户走验证码；需要密码在设置里自设。

### 2-3 日志明文手机号

文案：phone.py 三处 `logger.info(f"...{phone}")` 违反日志禁手机号红线。
修复：统一过 `_mask_phone`。

### 2-4 地图两接口绕过全部 LLM 配额

文案：限流/日配额只挂 chat 路径；`/api/map/recommend` 每请求一次 LLM、`/api/map/route` 同，登录用户可无限刷；`plan_route` 还在裸 `os.getenv("DEEPSEEK_API_KEY")` 取 key。
修复：两接口接入与 chat 同款的门禁（QPS/日配额检查）+ 统一走 `get_deepseek_key()`。

### 2-5 oauth 异常 str(e) 未脱敏拼进重定向 URL

文案：`RedirectResponse(f"...?error={str(e)}")` 把上游错误细节（可能含 URL/指纹）反射给前端且未转义。
修复：except 里映射为固定错误码（如 `github_callback_failed`），细节只进日志。

### 2-6 /metrics、/test-otel 应用层无鉴权

文案：lite 靠 nginx 404 兜底，直连容器端口即绕过；全量栈无此兜底。
修复：应用层加 admin 门禁，或部署上确认容器端口不对公网发布。

### 2-7 phone_login 不查 is_active

文案：禁用账号可凭短信再签发 token（get_current_user 有 403 兜底但用户缓存窗口 ≤5 分钟）。
修复：登录路径查到用户后校验 `is_active`，禁用即 403。

### 2-8 map_api 杂项五处

文案：`plan_route` 末尾 bare except（吞 BaseException）；`path.get("steps", [{}])[0]` 在 steps 为空数组时 IndexError；recommend 里 url/headers 构建后未用（死代码）；amap_route 5 连外调无显式超时；plan_route 未用 `_extract_json` 与 recommend 行为不一致。
修复：bare except 改窄化捕获；steps 取值改 `next(iter(path.get("steps") or []), {})`；删死代码；AsyncClient 显式 timeout；plan_route 补 `_extract_json` 容错。

### 2-9 change_password 不吊销旧会话

文案：改密后旧 refresh 最长 30 天有效。
修复：改密写 `auth:pwd_changed:{username}` 时间戳，refresh 流程发现签发早于该时刻一律拒绝（TTL 与 refresh 有效期一致）。

## 第 3 批：app/services

### 3-1 ensure_task_access 签名错位 → 任务三端点全 500

文案：`ensure_task_access(task, username, role)` 三参定义被 `(task, current_user)` 两参调用，运行时 TypeError（实测复现）→ `/v2/chat/tasks/{id}/result|cancel|resume` 每次请求 500。前端主用 SSE 所以没暴露。
修复：调用处补参；防回归加一个签名/调用一致性断言测试。

```python
# 错（app/routes/v2.py:112/125/138）
ensure_task_access(task, current_user)
# 对
ensure_task_access(task, current_user["username"], current_user["role"])
```

### 3-2 日 Token 计数无写入方 → 限额永不生效

文案：`update_daily_usage` 全仓唯一调用点写死 `inc_token=0`，`daily_token:*` 计数器无人写 → 日 token 门禁读到恒 0。usage 事件在 record_token_usage 里有，只是没接到计数器。
修复：`record_token_usage` 里顺带对 `daily_token:{user}:{date}` 做 INCRBY（与日请求数同一 Lua/键规则）。

### 3-3 today 本地时间 vs UTC 约定

文案：`ensure_chat_allowed` 用本地 `datetime.now()` 生成日期键，rate_limit 的 TTL 按 UTC epoch 日切且注释声明统一 UTC——容器外时区漂移时日切错位。
修复：统一 `datetime.now(timezone.utc).date()`。

### 3-4 conv_id 秒级时间戳碰撞

文案：无 conversation_id 时 `conv_{user}_{int(time.time())}`，同秒并发两条无 id 请求共用会话、历史互相污染。
修复：加 uuid 尾巴，如 `conv_{user}_{ts}_{uuid4().hex[:6]}`。

### 3-5 任务路径治理缺口

文案：任务创建无 QPS/日配额（仅全局 50 槽位）；resume 不复查 `is_quota_exhausted`；create 未持久化 lang，resume 恒回中文。
修复：create/resume 接入与 chat 同款门禁；resume 补额度复查；lang 写入任务 hash 并在 resume 透传。

### 3-6 cancel_agent_task 终态集合漏 "timeout"

文案：超时任务可被再置 cancelled。
修复：终态集合补 `"timeout"`。

### 3-7 Flash 降级链无全文 DFA 终检

文案：`fallback_flash` 逐块 check_stream 后没有收尾 `contains_sensitive` 终检——跨块拼接的敏感词在降级链可漏出（主链路 runner/ReAct 都有终检）。
修复：流尾对累积文本做一次 `contains_sensitive`，命中替换安全文案。

### 3-8 两处一致性

文案：`resume_agent_task(user_perms: list | None = [])` 可变默认参数；ReAct 工具参数兜底用 `ctx.req.query`、快速通信用 `ctx.user_query`（改写后），语义不一致。
修复：默认参数改 None；工具参数统一用改写后的 user_query。

## 第 4 批：app/models + app/agents

### 4-1 文档解析无页数/图片数/时长上限 → OCR 通道可被打瘫

文案：PDF 逐页渲染 150dpi + 逐图 OCR（全局 threading.Lock 串行化），无上限——1MB PDF 可塞上万页，单个恶意上传占住 OCR 通道小时级并占满 run_in_executor 默认线程池。Redis 侧 200KB 截断发生在解析完成后，救不了。
修复：页数/图片数硬上限 + 解析整体 deadline。

```python
if doc.page_count > MAX_PAGES:            # 例：50
    doc = doc[:MAX_PAGES]
text = await asyncio.wait_for(loop.run_in_executor(None, _extract_all), timeout=120)
```

### 4-2 _generate_embedding 硬编码 Ollama URL

文案：写死 localhost/host.docker.internal 两地址，`config.EMBEDDING_API_URL` 存在但此路径不读——改配置不生效。
修复：URL 从 config 读取，内置值仅作缺省。

### 4-3 ChatRequest.query 无 max_length

文案：单请求体积不受限（限流只管频率不管体积），超大 query 直达 LLM。
修复：`query: str = Field(..., max_length=8000)`（值可调）；file_ids 数量约束同步加在 schemas。

### 4-4 user_perms 可变默认参数

文案：`router.handle_simple_task`、`runner.run_agent_task` 的 `user_perms: list | None = []`（当前只读未触发，规范雷）。
修复：默认改 `None`，函数内 `user_perms = [] if user_perms is None else user_perms`。

### 4-5 orchestrator 并行 LLM 不走 llm_semaphore

文案：Phase1/Phase2 N 路并行调用绕过全局 LLM 并发控制（stream_llm 路径受控，此路径不受控）。
修复：gather 前按信号量包装每个协程。

```python
async def _bounded(coro):
    async with llm_semaphore:
        return await coro
tasks = [_bounded(c) for c in coros]
```

### 4-6 ILIKE 通配符未转义

文案：`_keyword_fill` 的 `%kw%` 中用户 `%`/`_` 不转义，可构造全表模糊匹配（性能面，参数化本身安全）。
修复：`kw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")` + ILIKE ... escape "\\"。

### 4-7 天气 lives 空数组 IndexError

文案：`weather_data["lives"][0]` 在 lives=[] 时炸，用户看到"天气查询失败"而非"暂无天气数据"。
修复：取前判空：`live = (weather_data.get("lives") or [{}])[0]`。

## 第 5 批：app/cdc + app/payment

### 5-1 充值订单退款 = 双倍入账，可无限刷余额

文案：`_settle_refund` 对一切可退订单执行 `_locked_wallet_change(+refund_amount)` 余额加钱。消费单 amount 为负被 `_decide_refund` 恰好拦住 → 实际可退的只有充值单——而充值单原充值已 +amount，退款再 +amount，同一笔充值双倍入账。`/api/payment/refund` 对所有登录用户开放、无频率限制、充值失败可同单重试 → 「充值→退款→循环」可把余额刷到任意数额。
修复：按原单类型定资金方向——充值单退款扣回钱包，消费单退款才加回；用户端禁止对充值单发起退款。

```python
# app/payment/refund.py:122（现状：方向错误）
updated, before, after, _ = await _locked_wallet_change(conn, user_id, refund_amount, tx_type="refund")
# 修复：充值单 → -refund_amount（total_recharged 反向累计）；消费单 → +refund_amount
delta = -refund_amount if locked["order_type"] == "recharge" else refund_amount
```

### 5-2 _record_transaction NameError 死代码

文案：函数体引用未定义的 `order_no`（`_ledger.py:278/280`），全仓无调用方但被 re-export 为公开符号。
修复：删除该函数（调用方都直接用 `_do_record_tx`）。

### 5-3 recover_stale_processing 扫描永不存在的状态

文案：扫 DB `status='processing'`，但全仓没有任何代码把订单写成 processing（该词只在 Redis 幂等占位用）→ 整个恢复函数是死的。
修复：删除；或让渠道调用前真落 processing 状态（与超时恢复配套），二选一。

### 5-4 扣费 0 余额边界不一致

文案：「余额不足按余额扣」条件是 `before_balance < cost and before_balance > 0`——余额 0.01 扣 0.01 归零，余额恰好 0 却扣全额转负，同为"不足"两种结果。
修复：去掉 `> 0` 条件，`actual_deduct = min(cost_amount, max(before_balance, 0))`。

### 5-5 deferred 钱包缺失毒丸

文案：`_settle_one` 钱包不存在 return False 永久重试，每维护轮刷一条 error，无作废路径。
修复：连续 N 轮失败后 `_void_order` 作废并告警。

## 第 6 批：frontend

### 6-1 本地会话不按用户隔离

文案：storage key 是 `gw_sessions_${personaId}`，共用浏览器换账号登录，前一用户聊天记录对新账号完全可见；登出不清会话/定位缓存。
修复：key 拼用户标识 `gw_sessions_${username}_${personaId}`；logout 时清除该用户全部会话键与定位缓存。

### 6-2 ChatRequest.user 塞 conversationId

文案：`api/chat.ts:46` 把会话 ID 塞进后端 `user` 字段（后端注释称"真实用户名由鉴权注入"）——两端语义错位，后端不消费。
修复：后端删除该字段（或前端传空），别让误导性字段存活。

### 6-3 注册协议默认勾选

文案：LoginView `agree = ref(true)`，后端"请同意用户协议"校验形同虚设。
修复：`ref(false)`。

### 6-4 GitHub 回调双重 saveTokens

文案：`auth.loginWithTokens` 内部已存，外面又显式存一次。
修复：删外层调用。

## 第 7 批：scripts / tests / alembic / deploy / tools

### 7-1 nginx /health 硬编码 200 → 发布健康门禁被架空

文案：`location = /health { return 200 ...; }` 不代理后端——发布脚本健康检查打在 10090 恒 200，网关挂了照样"健康"。
修复：代理真实健康端点。

```nginx
location = /health {
    proxy_pass http://gateway_cluster;
    proxy_connect_timeout 2s; proxy_read_timeout 3s;
}
```

### 7-2 `pytest tests` 收集坏死

文案：tests/ 混入直连真实 API 的一次性脚本（test_deepseek_direct.py import 期读 /app/.env），整套 pytest 收集即 FileNotFoundError（实测复现），156 个有效用例被淹没。
修复：一次性脚本挪 `tests/manual/`；pytest.ini 限定 `testpaths = tests/unit tests/integration`。

### 7-3 task_scheduler 越权校验可绕过

文案：`command[0]="python"` 无 .py 后缀无路径分隔 → 容器校验直接跳过；startswith 前缀无 os.sep 兜底。
修复：取 command 中首个脚本参数校验 + 前缀比对补 `os.sep`。

### 7-4 check_code_rules docstring 漂移

文案：文档称默认检查 app/ mcp_assets/ scripts/，实际 `DEFAULT_TARGETS = ["app"]`。
修复：二选一对齐。

## 第 8 批：langgraph_lab / mcp_assets / static

### 8-1 weather_demo 快照漂移

文案：lives[0]/缺字段问题未随主库修复（副本漂移案例 1）。
修复：首选改 `from app.agents.tools import fetch_weather_async`（方案 A）；已落【快照标注】+ test_snapshots.py 止血。

### 8-2 civil_code_server 快照漂移

文案：四步流水线是主库旧版快照；`except: pass` 静默、`__import__("json")` 内联导入。
修复：资产要求自包含展示，已落【快照标注】+ 纳入自检；`except Exception: pass` 补 debug 日志；对外演示说明"早期版本快照"。

### 8-3 import_civil_books import 期 pip install

文案：`os.system(f"{sys.executable} -m pip install ...")` 自动装包。
修复：改为依赖声明 + 启动时给出缺失提示，不自动装。

---

# 第三部分：历史踩坑搬运（48 条，源自 docs/踩坑与陷阱总汇.md）

## 一、环境与工具链（Windows / Docker / 终端）

1. **Docker 构建太慢**

   文案：`apt-get install tesseract-ocr` 国内网络极慢；`--no-cache` 全量重建每次重装 torch（20 分钟）。
   代码：

   ```dockerfile
   # 对：依赖层放在代码 COPY 之前，小改动走层缓存；小补丁用 docker cp + restart
   COPY requirements.txt .
   RUN pip install -r requirements.txt
   COPY app/ ./app/
   ```

2. **Docker 进程被 kill 后 CLI 不可用**

   文案：`taskkill /F /IM docker.exe` 把 CLI 一起杀掉，之后 `CommandNotFoundException`。
   代码：

   ```powershell
   Start-Service com.docker.service   # 对：重启服务而非杀进程
   ```

3. **WSL bash 写 Windows 盘 pyc 权限失败**

   文案：WSL 里 `python -m compileall` 写 `app/__pycache__` 被拒。
   代码：

   ```python
   import ast; ast.parse(source)   # 对：纯解析不落盘，任何环境兼容
   ```

4. **wslrelay 拦截 localhost:5432**

   文案：宿主连 `localhost:5432` 被 `wslrelay.exe` 拦截认证失败；容器 IP 从宿主不可达。
   代码：

   ```
   # 对：走局域网 IP（命中 com.docker.backend 的 0.0.0.0:5432）
   # 落地：migrate.py 自动探测（DB_HOST > localhost > 局域网 IP）
   ```

5. **持久终端偶发损坏 + 推送输出被吞**

   文案：终端越长寿越频繁 `^U` 前缀、git 找不到。根因：PowerShell 5.1 自带 PSReadLine 2.0.0 与 VS Code shell integration 的 OSC 序列冲突。
   代码：

   ```powershell
   Install-Module PSReadLine -RequiredVersion 2.4.5      # 解法：升级
   git push ... *> push.log; Get-Content push.log        # 确认 push 输出走文件
   ```

6. **Windows 中文路径 + venv ensurepip 失败**

   文案：`python -m venv` 在 `D:\桌面\...` 中文路径下 ensurepip 报错。
   代码：

   ```
   # 对：避开中文路径建 venv；或直接用已有解释器
   ```

7. **pip 安装被沙箱/权限拒绝**

   文案：pip 写系统 Temp 目录 Permission denied。
   代码：

   ```
   # 对：以完整权限执行安装；装包属一次性环境操作
   ```

## 二、编码与字符集

8. **UTF-8 BOM 导致 json.load() 失败**

   文案：PowerShell `Set-Content -Encoding UTF8` 写入 BOM，读取端炸。
   代码：

   ```python
   json.load(open(p, encoding="utf-8-sig"))   # 对：读容错 BOM
   ```

9. **PowerShell 管道传中文乱码（GBK）**

   文案：`@'...'@ | docker exec -i` 传中文按 GBK → 乱码。
   代码：

   ```bash
   # 对：中文代码先写 .py 再 docker cp 进容器执行
   docker cp fix.py agent_gateway:/tmp/fix.py
   ```

10. **控制台 GBK 显示乱码**

    文案：代码页 936 下 print 中文/emoji 报 UnicodeEncodeError。
    代码：

    ```powershell
    [Console]::OutputEncoding = [Text.Encoding]::UTF8
    $env:PYTHONIOENCODING = "utf-8"
    ```

11. **alembic.ini 中文注释炸（GBK）**

    文案：Windows configparser 按 GBK 读 ini，UTF-8 中文注释 UnicodeDecodeError。
    代码：

    ```ini
    # 对：ini 保持纯 ASCII 注释
    ```

12. **CRLF 换行符**

    文案：bash 脚本带 CRLF 在容器内炸（`\r` 被当命令一部分）。
    代码：

    ```bash
    sed -i 's/\r$//' deploy.sh   # 对：统一 LF；.gitattributes 里 *.sh text eol=lf
    ```

13. **env 文件尾部无换行 → 追加拼行**

    文案：`echo "K=V" >> env` 拼到上一行，密钥互相污染。
    代码：

    ```bash
    printf '\n%s\n' "K=V" >> .env   # 对：先补换行再追加
    ```

## 三、配置与密钥治理

14. **密钥硬编码 41 个文件**

    文案：Key/密码散落代码，仓库泄露=全部泄露。
    代码：

    ```python
    if not _settings.jwt_secret:                 # 对：统一收口 + fail loudly
        raise RuntimeError("JWT_SECRET 未设置！")
    ```

15. **configparser 遇密码 `%` 报错**

    文案：密码含 `%` 触发 invalid interpolation，转义也炸。
    代码：

    ```python
    f"postgresql://{user}:{quote_plus(pwd)}@{host}/db"   # 对：绕过 ini + quote_plus
    ```

16. **双 env 文件分叉（配置漂移变体）**

    文案：安全目录与根目录 .env 各缺各的键，改哪份都不生效。
    代码：

    ```yaml
    env_file:
      - "\\wsl.localhost\\Ubuntu\\root\\agent-gateway\\.env"   # 对：统一指权威源
    ```

    补充权限坑：安全目录文件必须 644——600 会让经 UNC 读取的 compose/脚本全部 PermissionError。

17. **配置漂移导致线上全链路故障（真实事故）**

    文案：容器内密码被外部改掉、git 配置仍旧 → 4 网关连库失败 → 登录 500 → 支付全链路挂。
    代码：

    ```bash
    docker exec postgres env | grep POSTGRES   # 排查：看容器真实环境变量 ≠ 配置文件
    ```

18. **docker compose env_file 可直接读 UNC 路径**

    文案：compose CLI 在 Windows 侧运行，`\\wsl.localhost\...` 正反斜杠都认——密钥可只存 WSL 安全目录不落项目。

19. **env_file + `${VAR}` 取空值**

    文案：注入方式没端到端验证，容器内取到空。
    代码：

    ```bash
    docker compose config | grep -A2 environment   # 对：看注入后的实际值
    ```

## 四、数据库与 PostgreSQL

20. **asyncpg 时区类型不兼容**

    文案：offset-aware datetime 写不进 TIMESTAMP（naive）列。
    代码：

    ```python
    def _utcnow():
        return datetime.now(timezone.utc).replace(tzinfo=None)   # 对：naive UTC
    ```

21. **中文/特殊字符密码 URL 编码**

    文案：密码含 `#` `@` 在连接串/Shell 传参中炸。
    代码：

    ```python
    f"postgresql://{user}:{quote_plus(pwd)}@{host}:5432/{db}"   # 对：quote_plus
    ```

22. **慢查询拖垮连接池**

    文案：慢查询占住连接 → 池耗尽 → 全站变慢。
    代码：

    ```python
    async with pool.acquire(timeout=5) as conn:   # 对：获取带超时，with 保证归还
        ...
    ```

23. **重复数据导入（md5 幂等失效场景）**

    文案：docx 与库内 content 微差（换行）→ md5 不同 → 重复 chunk。
    代码：

    ```sql
    -- 对：按 chunk_key 去重；清理时"保留带 embedding 的，其次内容长的"
    ```

## 五、Python 与异步

24. **协程里阻塞调用**

    文案：`time.sleep`、同步 requests 卡死事件循环。
    代码：

    ```python
    await asyncio.sleep(0.1)                    # 对：异步等待
    raw = await asyncio.to_thread(sync_fn)      # 对：同步阻塞放线程池
    ```

25. **异步落盘无保护**

    文案：create_task 写库无限流 → 高并发打满连接池。
    代码：

    ```python
    _SEM = asyncio.Semaphore(5)
    async def _save():
        async with _SEM:            # 对：信号量限流 + 异常兜底
            await pool.execute(...)
    ```

26. **逐条 INSERT + 远程探测太慢**

    文案：每条记录先探测 embedding 服务再插入，300 条超时。
    代码：

    ```python
    await conn.executemany("INSERT ... VALUES ($1,$2)", batch)   # 对：一次探测+批量
    ```

## 六、AI / Agent 与检索

27. **模型级问题硬改 prompt 无效**

    文案：模型顽固把"七天无理由退货"归民法典，改 prompt 无效。
    代码：

    ```python
    hit = law_mapping.check_query(query)   # 对：架构绕过——纠正映射表直接返回引导
    if hit: return {"mapping_hit": True, "message": hit["message"]}   # CC019 2分→7分
    ```

28. **知识库构建正则漏导千位条号（重大缺陷）**

    文案：条号正则缺"千""零" → `第一千零四十条` 起全部漏导，第五~七编整编缺失。
    代码：

    ```python
    # 错：r"第[一二三四五六七八九十百]+条"
    r"第[一二三四五六七八九十百千万零]+条"   # 对
    ```

29. **pg_trgm `%` 对中文短查询失效 + 长句噪音**

    文案：6 字查询对长法条相似度趋 0；映射后长句又召回噪音占满配额。
    代码：

    ```sql
    -- 对：相似度全量排序（<0.1 丢弃）+ ILIKE 子词补充无条件执行
    SELECT heading, content, similarity(content, $1) AS sim
    FROM knowledge_chunks WHERE source='civil_code'
    ORDER BY sim DESC LIMIT $2;
    ```

30. **docx 是"目录 + 正文"双结构**

    文案：编标题出现两次（目录区+正文区），统计被误导。
    代码：

    ```python
    # 对：以最后一次编标题为准（正文区），统计前先验证分布
    ```

31. **语义缓存 L0 清不掉**

    文案：清 PG 表不清进程内 LRU（256 条）→ 仍命中旧缓存。
    代码：

    ```bash
    docker restart agent_gateway   # 或等 LRU TTL 自然淘汰
    ```

32. **LLM 返回非法 JSON**

    文案：子 Agent 首轮返回非纯 JSON。
    代码：

    ```python
    # 对：多级提取（直接解析 → ```json 块 → 花括号 → 修未引用 key）+ 重试一次
    ```

## 七、MCP 专项

33. **stdio 传输依赖命名管道，受限环境被拒（WinError 5）**

    文案：沙箱/受限环境禁止创建命名管道，`stdio_client` 拉起子进程即 PermissionError——协议特性，不是代码问题。
    代码：

    ```
    # 对：以完整权限运行 MCP 实验
    ```

34. **子进程 args 必须绝对路径**

    文案：相对路径 `weather_server.py` 按父进程 cwd 解析失败（No such file）。
    代码：

    ```python
    server = os.path.join(os.path.dirname(os.path.abspath(__file__)), "weather_server.py")
    ```

35. **Resource URI scheme 必须合法（RFC 3986）**

    文案：`civil_code://coverage` 的 scheme 含下划线 → pydantic AnyUrl 校验失败，Server 启动即崩。
    代码：

    ```python
    @mcp.resource("civil://coverage")   # 对：scheme 只用字母数字 + - .
    ```

36. **封装"能力"不是封装"流程"**

    文案：把"映射→召回→Rerank→生成回答"打成一个黑盒 tool → Host 失去干预点/成本可见性/可观测性。
    代码：

    ```python
    # 对：Tool = 原子动作（search_civil_code 只做检索）；
    # 领域知识下沉 Server，生成回答留 Host，多 Agent 圆桌是编排逻辑不封装成 tool
    ```

37. **参数暴露是取舍不是越多越好**

    文案：参数越多 Host 越灵活，但模型越容易用错。
    代码：

    ```python
    async def search_civil_code(query: str, top_k: int = 5, use_rerank: bool = True):
        ...  # 对：暴露 top_k/use_rerank；Rerank 阈值等内部策略默认值收在 Server
    ```

38. **依赖治理：自包含 vs 公共库**

    文案：MCP Server 直接 import 项目内部模块会拉起整个应用初始化（连接池/日志/配置）。当时取舍是自包含（只复用零依赖 `_env.py` + 数据文件）、逻辑复制两份——这正是第一部分「副本漂移」的温床：复制时就该同时落快照标注或规划抽库时点。
    代码：

    ```python
    # 现状（漂移温床）：流水线手工快照 + 仅共享 _env.py
    sys.path.insert(0, str(_ROOT)); from _env import get, db_url
    # 演进方向：能 import 抽共享层；必须独立则【快照标注】+ tests/test_snapshots.py
    ```

## 八、部署与运维

39. **滚动发布 vs 全量重建**

    文案：4 实例同时 force-recreate = 上线瞬间全站停摆。
    代码：

    ```bash
    # 对：一次一个实例，健康检查过再下一个；记录 PREV_IMAGE 便于回滚
    ```

40. **Docker DNS 不解析已停服务 → nginx reload 失败**

    文案：upstream 引用已停容器，reload 报 host not found，旧配置继续生效，轮询到死实例超时（login 499）。
    代码：

    ```nginx
    proxy_connect_timeout 2s;
    proxy_next_upstream error timeout http_500 http_502 http_503 http_504;
    proxy_next_upstream_tries 4;   # 对：单实例故障可跳过
    ```

41. **docker compose 与手动容器命名冲突**

    文案：`container name already in use`，`set -e` 中断整个脚本。
    代码：

    ```bash
    docker compose up -d || echo "WARN: 已在运行，继续"   # 对：基础设施步骤容错
    ```

42. **压测忘设 --run-time**

    文案：压测无限跑。工具使用也要有熔断意识。
    代码：

    ```bash
    locust -f locustfile.py --run-time 5m --users 50   # 对：显式限时
    ```

43. **索引选型**

    文案：数据量上来后毫秒→秒级（全表扫描退化）。
    代码：

    ```sql
    CREATE INDEX ON knowledge_chunks USING gin (content gin_trgm_ops);  -- 模糊
    CREATE INDEX ON knowledge_chunks USING ivfflat (embedding vector_cosine_ops); -- 向量
    EXPLAIN ANALYZE SELECT ...;   -- 对：任何索引以执行计划验证为准
    ```

## 九、自检与测试

44. **自检断言凭印象写数字，必挂**

    文案：凭感觉断言「整体 8.4 >= 8.0」，实际 8.17——断言与真实计算不符，第一版必挂。
    代码：

    ```python
    print(gate_failures(...))   # 对：先跑真实输出 → 再固化 assert
    ```

45. **纯函数参数顺序不清 → 断言写反**

    文案：overall 传成 8.5、threshold 传 8.0，断言"整体线失败"永不成立——参数错位测了错误场景。
    代码：

    ```python
    gate_failures(overall_avg=8.17, threshold=8.0)   # 对：关键字传参 + 注明传入/期望
    ```

46. **测试没走业务同一路径（测试与实现分叉）**

    文案：自检直接用 `L0.get()` 断言 TTL，业务路径却是 `_l0_get()`（带过期淘汰）——测了个寂寞。
    代码：

    ```python
    # 对：抽公共函数，自检调用与业务完全相同的入口；分叉 = 假绿
    ```

## 十、MCP 客户端编程

47. **stdio 会话清理期抛 ExceptionGroup 掩盖真实异常**

    文案：阶段断言失败后，`async with stdio_client(...)` 退出时 TaskGroup 抛 ExceptionGroup 顶掉真正的 AssertionError，报告只见「执行异常」。
    代码：

    ```python
    # 对：阶段异常在上下文内部先捕获记录，退出后再上报；
    # 清理期异常仅在无阶段异常时才算失败
    ```

48. **工具里资源获取必须在 try 内（错误返回体化纪律）**

    文案：`get_redis()` 放 try 外——缺包/连接失败抛异常，违反「错误返回 {"error": ...} 而非抛异常」纪律。
    代码：

    ```python
    @mcp.tool()
    async def tool_x(...):
        try:
            r = await get_redis()      # 对：资源获取与业务一起包 try
            ...
        except Exception as e:
            return {"error": str(e)}
    ```

---

*维护约定：新 bug 按「遇到的 bug → 怎么修复」追加；模式级问题优先记入第一部分。*
