
文件：app/core/db.py
级别	行号	问题描述	修复建议
P0	12-16	未校验 min_size > max_size，可能引发 asyncpg 创建池失败但错误信息被吞	添加校验并抛出明确 ValueError
P0	23-29	create_pool 未显式设置 timeout、max_inactive_connection_lifetime，生产环境连接可能被无限占用或超时	从 config.DB_ACQUIRE_TIMEOUT 读取 timeout，并设置 max_inactive_connection_lifetime=300
P1	40-44	get_db_conn 作为 FastAPI 依赖，若业务代码在 yield 后抛出异常，连接可能未正确归还	改用 @asynccontextmanager 并捕获异常以确保释放
P2	23	初始化失败时仅记录日志，未区分是否应退出进程（如致命错误）	根据环境变量决定是否 raise，测试环境可容错
文件：app/core/redis.py
级别	行号	问题描述	修复建议
P0	19-21	max_connections=20 硬编码，且未设置 pool_timeout，高并发下获取连接可能阻塞无限	从环境变量读取，并设置 pool_timeout=5.0
P0	23-27	init_redis 中 ping() 失败仅告警，未抛出异常，导致后续 get_redis() 返回未初始化对象，引发 NoneType 错误	改为 raise RuntimeError，让服务启动失败
P1	8-11	_build_redis_url 简单清理 @ 前的空认证，但若包含 : 且无密码时可能误判	使用 urllib.parse.urlparse 标准化解析
P2	19	未设置 retry_on_error 策略（如网络抖动自动重试）	添加 retry_on_error=[ConnectionError, TimeoutError]
文件：app/core/config.py
级别	行号	问题描述	修复建议
P0	全局	无 Pydantic 校验，所有值均为原始字符串，类型转换松散（如 float(os.getenv(...)) 可能抛出未捕获异常）	引入 pydantic_settings.BaseSettings 统一校验
P0	48-50	ADMIN_PASSWORD 未强制非空，生产环境若忘记设置则 admin 密码为空，存在安全风险	启动时检查，若为空且非测试环境则抛出 RuntimeError
P0	73	CORS_ORIGINS 直接用逗号分隔，未 trim 空格，且未对空字符串过滤，可能导致 CORS 配置异常	使用 [o.strip() for o in os.getenv(...).split(",") if o.strip()]
P1	9	POSTGRES_DSN = os.getenv("DATABASE_URL") or "" — 空值会导致 db.py 报错信息不明确	在 config 层直接校验，若空则抛出友好错误
P1	44-45	ALIBABA_CLOUD_ACCESS_KEY_SECRET 有 .strip(' \"\'') 操作，但若值包含空格（正常不会），可能误删	建议只做简单的 strip()，保留内部空格
P2	23-24	新旧密钥轮换逻辑（SECRET_KEY_OLD）存在但未实现自动轮换脚本	在文档中补充轮换流程，或增加 scripts/rotate_jwt_secret.sh 说明
文件：app/core/logging.py
级别	行号	问题描述	修复建议
P1	23-25	_get_trace_id() 每次调用都通过 OpenTelemetry API 获取，性能开销大（每条日志都有）	使用 contextvars 在请求入口设置 trace_id，日志直接从上下文中读取
P1	27-36	JsonFormatter.format() 每次调用 datetime.now(timezone.utc)，可改用 record.created 减少系统调用	"time": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
P2	33	_fast_dumps 依赖 orjson，若 orjson 不可用，降级到标准库，但未记录降级日志	在 jfast.py 中增加 logger.warning 提示降级
文件：app/core/metrics.py
级别	行号	问题描述	修复建议
P1	3-5	endpoint 标签若包含动态路径（如 /users/123），将导致标签爆炸，消耗大量内存	在中间件中归一化路径（替换数字段为 :id），或使用路由的 route.path
P1	43-44	llm_tokens_detail 标签 ['model', 'endpoint', 'type'] 中 endpoint 若为 v2_chat，基数有限，可控	无需修复，但需注意后续扩展
P2	54-55	semantic_cache_hits_total 和 misses_total 未加 method 标签区分操作类型（如 get/set）	可增加标签，但非必须
文件：app/core/task_manager.py
级别	行号	问题描述	修复建议
P0	119-127	_idempotent_map 是进程内字典，多实例部署无法共享幂等；且 call_later 在异步上下文中可能因事件循环未启动而报错	改用 Redis 存储，带 10s TTL，并使用 asyncio.create_task 配合 sleep 清理，或直接设置 EX=10
P0	25	TASK_TTL = 600 硬编码，若任务处理超过 10 分钟，Redis 中任务被清除，但后台仍在运行，状态丢失	移至 config.py 并可调大默认值
P1	74-78	append_result 使用 HINCRBYFLOAT 记录长度，但长度是整数，应使用 HINCRBY	改为 await r.hincrby(f"task:{task_id}", "_result_len", len(chunk))
P1	68-70	update_status 中若 result 为空则跳过更新，但 result 可能为 None，if result: 会跳过，但未设置 result 字段可能导致旧值残留	显式设置 mapping["result"] = result if result is not None else ""
P2	14	状态机只定义了 pending/generating/completed/cancelled/error，但未定义 timeout 状态，若任务超时应在外部检查	建议增加 timeout 状态，并在外部定时任务中清理
文件：app/core/memory_manager.py
级别	行号	问题描述	修复建议
P0	117-122	_update_profile_async 在 _save_to_pg 的事务内部执行，若画像更新失败（如 JSON 异常），整个事务回滚，导致对话记忆也丢失	将画像更新移出事务，或使用独立的 conn.execute 且不依赖事务
P1	13	_PG_WRITE_SEMAPHORE = 12 硬编码，应与 config.DB_POOL_MAX_SIZE 联动	设为 min(12, int(os.getenv("DB_POOL_MAX_SIZE", 50)) // 4) 或从配置读取
P1	93-99	save_user_location 使用 asyncio.create_task 创建后台任务，未绑定异常回调，异常静默丢失	添加 task.add_done_callback 记录错误
P1	147-152	get_profile 中查询到过期画像时执行 DELETE，但未使用事务，可能与其他并发操作冲突	使用 DELETE ... RETURNING 确保原子性，或使用 SELECT ... FOR UPDATE
P2	138-142	正则 re.findall(r'([\u4e00-\u9fa5]{2,4}(?:市|州|省|区))', user_content) 无法匹配「内蒙古自治区」（7字），且「北京市」匹配为「北京市」3字，可接受	可优化为更宽泛匹配，但当前能满足基本需求
文件：app/core/semantic_cache.py
级别	行号	问题描述	修复建议
P0	76-86	L2 语义匹配使用 similarity(query_text, $1) 未触发 GIN 索引，大表全表扫描性能极差	创建 GIN 索引并使用 % 运算符：WHERE query_text % $1 AND similarity(...) > $2
P0	167-178	分布式锁 acquire_rebuild_lock 无续期，重建任务若超过 TTL（45s）锁自动释放，导致击穿	使用续期机制（如开启后台任务每 15s 续期）或使用 Redlock 方案
P1	49	_LRUCache 的 default_ttl=600 硬编码	从 config.CACHE_L0_TTL 读取
P1	146-147	set 方法中 if not response or not str(response).strip(): 若 response=None，str(None) 为 "None" 非空，会误写入	改为 if response is None or not str(response).strip():
P1	107	set_empty 写入 __EMPTY__ 占位，但未设置 PG 中该记录的 hit_count=0，且 query_text 可能重复	已使用 ON CONFLICT 处理，但建议显式重置 hit_count=0
P2	158	release_rebuild_lock 使用 Lua 脚本，但 Redis 连接可能超时，导致锁未释放	可增加重试逻辑，但非必须
文件：app/core/safety_filter.py
级别	行号	问题描述	修复建议
P1	18-19	敏感词库硬编码，修改需重新部署	改为从外部文件（如 sensitive_words.txt）加载，支持热更新
P1	69-73	_in_safe_context 使用子串匹配，可能误判（如「天安门」出现在非法上下文中）	可改用正则边界匹配，但当前风险可控
P1	85-90	超时机制每字符检查时间，大文本（>10k 字符）开销较大	限制最大扫描长度（如 5000 字符），超出直接截断
P2	136-139	find_first 返回元组，但未处理 text 为空的情况	若 text 为空，直接返回 None（已处理，但建议显式检查）
文件：app/core/quota.py
级别	行号	问题描述	修复建议
P0	37-43	inc_used_questions 使用 UPDATE ... SET used_requests = used_requests + 1 无条件更新，高并发下可能导致超限（两次并发都读取到19，各自+1，最终21，但限额20）	将 used_requests < GITHUB_QUESTION_LIMIT 加入 WHERE 条件，并检查受影响行数
P1	13-15	is_quota_exhausted 仅比较 used_requests >= limit，若 used_requests 因并发超限，返回 True 后仍可能继续，因为判断与更新不同步	结合上述修复，使用原子检查
P2	20-24	remaining_questions 返回 -1 表示无限制，但调用方可能误判，建议返回 float('inf') 或 None	可优化为 Optional[int]
文件：app/core/persona_manager.py
级别	行号	问题描述	修复建议
P1	34-38	_load_personas 若 prompts/ 目录不存在或所有文件解析失败，_personas 为空，后续 current 为 None，调用 get_system_prompt 返回空字符串，可能导致 LLM 无 system prompt	增加默认人格（如统一助手）的降级逻辑
P1	82-86	全局单例 _manager 在首次调用时初始化，若初始化失败（如文件读取异常），全局状态异常	使用工厂模式，增加重试或初始化时捕获异常并设置默认值
P1	101-103	switch 仅改内存状态，未持久化，重启后丢失	若需要跨请求记忆，将当前人格存到 Redis 或 session
P2	59-62	get_persona_prompt 使用 _PERSONA_CACHE 全局字典，但 PersonaManager 也缓存了 _personas，存在双缓存不一致风险	统一使用 PersonaManager 的缓存，或让 get_persona_prompt 直接调用 PersonaManager
文件：app/core/jfast.py
级别	行号	问题描述	修复建议
P2	7-8	ensure_ascii 参数被忽略，但函数签名保留，调用方可能传入依赖该参数	文档中说明该参数无效，或使用 **kwargs 忽略
P2	21-25	降级到标准库时未记录日志，难以发现性能问题	增加 logging.warning
P2	16	orjson.dumps 不支持 default 回调，若对象含不可序列化类型会抛出 TypeError，但标准库可处理	可增加 default 参数支持或文档说明
文件：app/core/stream_utils.py
级别	行号	问题描述	修复建议
P1	97-107	_extract_city 仅精确匹配 CITIES，不支持别名（如「帝都」→北京）或拼写错误	增加别名映射，或使用模糊匹配（如 if c in user_query 已实现，但「北京」在「北京市」中也会匹配，但 CITIES 只含「北京」，不含「北京市」，所以若用户写「北京市」则无法匹配）—— 建议将「北京」也加入 CITIES 或使用包含关系
P1	134-140	sanitize_uploaded_content 使用正则替换，但存在替换后内容仍可能被利用的风险（如替换后组合成新注入）	当前为轻量防御，可接受；建议增加更多模式，或完全过滤可疑字符
P2	43-44	stream_llm 中 temperature=0.3 硬编码默认值，应可从 config 或请求参数传递	已支持 temperature 参数，但默认值建议从 config.LLM_TEMPERATURE 读取
P2	79-83	dispatch_tool 中的 web_search 和 search_project_knowledge 未检查权限，可能存在越权风险	当前仅 search_knowledge 有 permissions，建议统一增加权限校验
文件：app/core/constants.py
级别	行号	问题描述	修复建议
P1	46	get_persona_prompt 使用 open(path, "r", encoding="utf-8-sig")，若文件为 GBK 编码会失败	增加异常捕获，尝试 locale.getpreferredencoding() 或 chardet
P1	49-52	get_all_persona_ids 使用 glob("*.json")，若目录中有非 JSON 文件会忽略，但若目录不存在则返回 []，未告警	增加日志提示
P2	3-5	CITIES 列表庞大，建议外部化到 JSON 文件，便于维护	可移至 data/cities.json
额外文件（不在审查清单，但被包含，简要指出）
agent_rules.py：正则模式 CODE_ARCHITECTURE_PATTERN 包含中文词，但 re.IGNORECASE 仅对 ASCII 有效，中文不受影响，无问题。

audit.py：脱敏逻辑遍历 detail 键，若 detail 是嵌套字典，无法递归脱敏，存在泄露风险。但当前场景简单，可接受。

cache_warmup.py：硬编码查询列表，但 SemanticCache.get 可能因 PG 连接失败而抛出异常，预热失败不影响启动，可接受。

concurrency.py：llm_semaphore 使用全局信号量，但若 LLM_MAX_CONCURRENCY 被设为负数，会出错，建议校验。

db_maintenance.py：压缩时使用 conn.transaction() 但未设置隔离级别，可能与其他操作冲突，建议使用 SERIALIZABLE 或 REPEATABLE READ。

error_aggregator.py：Redis 聚合写入失败仅警告，不影响主流程，合理。

plugin_loader.py：importlib.import_module 使用绝对模块路径，若工具在 tools/ 子包中需配置正确，未做错误恢复。

slow_query_watch.py：使用 Redis 分布式锁 set(nx=True, ex=interval)，但锁未续期，若检查任务执行超过 interval，锁可能提前释放，导致多个实例同时查询。但 check_once 通常很快，风险低。

sms.py：签名算法正确，但 ALIBABA_CLOUD_ACCESS_KEY_SECRET 可能包含特殊字符，_percent_encode 对 secret 未编码可能导致签名错误（但已正确处理）。建议增加单元测试。

文件：app/core/agent_rules.md
说明：此文件为 agent_rules.py 引用的规则文档（Markdown 格式），agent_rules.py 会优先读取本文件内容，若不存在则回退到硬编码的 CODE_ARCHITECTURE_RULES。

审查结论
该文档本身无代码级别硬伤，内容清晰、结构合理。但结合 agent_rules.py 的使用方式，存在以下设计与一致性问题：

级别	行号	问题描述	修复建议
P1	全文	agent_rules.py 中硬编码的 CODE_ARCHITECTURE_RULES 与本 agent_rules.md 内容不一致（硬编码版本更简洁，文档版本更详细）。若文档存在但内容被修改，而硬编码版本未同步，会导致行为不一致——读取文件与回退到硬编码时规则不同，造成不可预测性。	方案 A：删除 agent_rules.py 中的硬编码回退，若文档缺失则直接抛出异常或返回空字符串，确保单一数据源。
方案 B：若必须保留硬编码回退，则在 agent_rules.md 头部显式注明“此文件与 agent_rules.py 中的回退规则必须保持同步”，并增加 CI 检查。
P2	24	文档末尾包含路径信息 路径：app/core/agent_rules.md，该路径与文件实际位置一致，但若项目重构移动文件，该路径会成为过时信息。	移除文件内的路径引用，或改为相对路径 ./agent_rules.md，并添加注释说明“此路径仅供参考，代码以运行时查找为准”。
P2	全文	规则文档中“禁止在以下日常任务中读取本规则”与“仅在以下类型的问题中读取”存在重叠，但未明确界定边界。例如“系统设计与服务拆分”和“API 设计”属于技术类，“旅游规划”属于非技术类，边界清晰，无歧义。	无需修改，但建议在文档开头增加一句明确的触发条件总结，如“触发条件：用户问题中包含技术工程词汇（架构/设计/代码/部署/性能/重构/CI/CD 等）”。
P2	11-13	规则中要求“不应影响日常业务场景”，但 agent_rules.py 中 is_code_architecture_task 仅通过关键词匹配判断，存在误判可能（如“我想去北京旅游，查一下天气和酒店推荐”会命中“天气”“酒店”但不会命中技术关键词，可正常绕过）。当前方案可接受。	无修复必要，但建议在文档中说明判断依据是关键词匹配，提醒维护者不要添加与业务场景重叠的关