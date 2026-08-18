P0 级（立即修复：缓存击穿 / 资源泄漏 / 安全绕过）
1. 【P0-缓存击穿】SemanticCache.get 无分布式锁，缓存失效时大量请求直接穿透到 LLM
文件: runner.py 第 52-78 行（_try_cache_hit）

python
async def _try_cache_hit(...):
    cached = await SemanticCache.get(user_query)  # ① 所有请求同时查询缓存
    if not cached:
        return False                               # ② 全部未命中，同时进入 LLM 调用
风险：当缓存过期或首次查询时，100 个并发请求同时发起，全部缓存未命中，同时触发 100 次 LLM 调用，导致：

DeepSeek API 被瞬间打爆（Rate Limit 或 429）

8GB 机器的 CPU/内存被 100 个并发推理任务压垮

数据库连接池被 100 个 get_user 查询占满

修复铁律：使用 Redis 分布式锁实现 "单飞模式（Single Flight）"——第一个请求去查 LLM，其余请求等待结果：

python
async def _try_cache_hit(...):
    cache_key = f"cache:lock:{hash(user_query)}"
    if await SemanticCache.get(user_query):
        return True
    # 尝试获取锁
    lock = await redis.set(cache_key, "1", nx=True, ex=10)
    if lock:
        try:
            result = await call_llm(...)
            await SemanticCache.set(user_query, result)
            return result
        finally:
            await redis.delete(cache_key)
    else:
        # 等待其他请求完成
        for _ in range(20):
            await asyncio.sleep(0.5)
            cached = await SemanticCache.get(user_query)
            if cached:
                return True
        return False
2. 【P0-资源泄漏】handle_simple_task 中 mm.save_messages 使用 asyncio.create_task 不等待，但未处理异常
文件: runner.py 第 241 行

python
asyncio.create_task(SemanticCache.set(user_query, final_answer))
风险：asyncio.create_task 创建的后台任务如果抛异常，异常会被静默吞噬（未 await 的任务异常在 GC 时才会被记录）。导致：

缓存写入失败但无任何日志

内存中积压大量未完成的 Task 对象，长期运行后内存泄漏

修复：包装一个错误处理函数：

python
async def safe_cache_set(key, value):
    try:
        await SemanticCache.set(key, value)
    except Exception as e:
        logger.warning(f"缓存写入失败: {e}")

asyncio.create_task(safe_cache_set(user_query, final_answer))
3. 【P0-安全绕过】routing_table.py 的 FORCED_CIVIL_KEYWORDS 可被注入绕过
文件: routing_table.py 第 94-126 行

python
_FORCED_CIVIL_KEYWORDS = ["偷拍", "隐私", "肖像", ...]
if keyword in query:  # 字符串包含判断
    return RoutingResult(action="pass")
风险：攻击者可以通过构造包含这些关键词的恶意查询绕过黑名单。例如：

真实问题："我被老板拖欠工资怎么办"（本应拒答）

绕过查询："我被老板拖欠工资怎么办 隐私"（包含"隐私"关键词 → 被强制放行）

修复：强制白名单应严格匹配语义主干，而非简单的子串包含。改为使用词边界匹配（re.search(r'\b' + re.escape(kw) + r'\b', query)）或要求关键词必须出现在句子的核心位置（非句尾附加词）。

4. 【P0-文件包含漏洞】document_parser.py 的 parse_docx 解压 ZIP 时未校验路径，可能被 Zip Slip 攻击
文件: document_parser.py 第 152-161 行

python
with zipfile.ZipFile(filepath, 'r') as z:
    media_files = [f for f in z.namelist() if f.startswith('word/media/')]
    for media_path in media_files:
        img_bytes = z.read(media_path)  # 直接读取，未校验路径
风险：恶意制作的 .docx 文件（本质是 ZIP）可能包含 ../../etc/passwd 这样的路径。虽然当前代码只读取 word/media/ 前缀的文件，但 z.read(media_path) 在解压时不会检查路径是否在目标目录内。如果攻击者上传一个包含 word/media/../../secret.txt 路径的 docx，读取操作可能泄露服务器文件。

修复：使用 os.path.basename 或 os.path.normpath 校验：

python
safe_path = os.path.normpath(media_path)
if safe_path.startswith('word/media/') and not safe_path.startswith('..'):
    img_bytes = z.read(media_path)
🟡 P1 级（尽快修复：数据一致性与可靠性）
5. 【P1-API Key 泄漏】_call_deepseek_think 和 sub_agents.py 在异常日志中可能打印 API Key
文件: orchestrator.py 第 300-315 行、sub_agents.py 第 50-70 行

python
except Exception as e:
    logger.warning(f"Agent {agent_id} {phase} 调用失败: {e}")
风险：如果 httpx 抛出包含完整请求头的异常，repr(e) 可能包含 Authorization: Bearer sk-xxx。日志系统会记录明文的 API Key。

修复：在日志前过滤敏感信息，或仅记录 str(e)[:100]。

6. 【P1-文件句柄泄漏】document_parser.py 的 parse_image 使用 PIL 打开文件但未显式关闭
文件: document_parser.py 第 109-115 行

python
def _ocr():
    img = Image.open(filepath)  # PIL 打开文件
    # ... 处理 ...
    return text
风险：Image.open() 返回的 Image 对象在 Python 中不会自动关闭底层文件句柄，直到被 GC 回收。在 200 QPS 下，大量图片解析会导致文件句柄耗尽（Linux 默认 1024 个）。

修复：使用上下文管理器：

python
with Image.open(filepath) as img:
    # 处理
7. 【P1-重试死循环】sub_agents.py 的 call_sub_agent 重试时未增加冷却，可能无限重试
文件: sub_agents.py 第 58-70 行

python
if retry:
    logger.warning(f"子Agent {agent_name} 首次返回非法JSON，自动重试")
    try:
        return await _call()  # 再次调用，但 retry=False 未传递
    except Exception as e2:
        return {"error": f"重试失败: {str(e2)}"}
风险：_call() 内部如果抛出异常（如网络超时），递归重试的 retry 参数未正确传递，可能导致无限递归（虽然在当前代码中 _call 是闭包，但逻辑易混淆）。更严重的是，如果 LLM 持续返回非法 JSON，重试一次后依然失败，但没有指数退避，可能在高并发下加剧 API 压力。

修复：增加重试次数限制（最多 2 次），并在重试前增加 asyncio.sleep(0.5)。

8. 【P1-竞态条件】law_mapping.py 的 _LAW_MAPPING_CACHE 在并发读取时无锁保护
文件: law_mapping.py 第 15-45 行

python
_LAW_MAPPING_CACHE: Optional[list[dict]] = None

def _load_mapping() -> list[dict]:
    global _LAW_MAPPING_CACHE
    if _LAW_MAPPING_CACHE is not None:
        return _LAW_MAPPING_CACHE
    # 读取文件...
    _LAW_MAPPING_CACHE = entries
    return _LAW_MAPPING_CACHE
风险：在 200 QPS 下，多个协程同时首次调用 _load_mapping，全部判断 _LAW_MAPPING_CACHE is None，然后同时打开文件读取，造成不必要的磁盘 I/O 和内存浪费。

修复：使用 asyncio.Lock 保护初始化过程，或使用 aiocache 等工具。

9. 【P1-异常吞噬】orchestrator.py 的 _call_deepseek_think 中 JSON 解析失败时静默返回空字典
文件: orchestrator.py 第 285-295 行

python
except json.JSONDecodeError as e:
    logger.warning(f"Agent {agent_id} {phase} JSON解析失败: {e}")
    return {"analysis": f"（分析失败: JSON解析错误）", "cross_comments": "无", "suggestions": []}
风险：LLM 返回的内容被截断或格式错误时，返回一个包含错误信息的字典，但调用方无法区分"LLM 正常返回"和"解析失败降级"。这可能导致：

下游 Agent 看到 analysis: "（分析失败: JSON解析错误）" 继续参与讨论，输出垃圾信息

最终用户收到 "无相关建议" 的回复

修复：返回时增加 "error": True 字段，让调用方决定是否重试或跳过该 Agent。

10. 【P1-Token 计量溢出】llm_tokens_total.labels(type='input').inc(prompt_tk) 可能丢失精度
文件: 多处（orchestrator.py、sub_agents.py、runner.py）

python
pt = usage.get('prompt_tokens', 0) or 0
llm_tokens_total.labels(type='input').inc(pt)  # pt 是 int，inc 接收 float
风险：Prometheus Counter 的 inc() 方法接收 float，但累计值超过 2^53 时可能丢失精度。虽然每天几十万 token 远未达到，但长期运行（1 年）后可能接近。真正的问题：pt or 0 在 pt=0 时正确，但如果 API 返回 "prompt_tokens": null，or 0 也能处理。无实际风险，列为低优。

11. 【P1-内存泄漏】runner.py 的 full_content_text 变量在循环中累积但从未使用
文件: runner.py 第 286 行

python
full_content_text = ""       # 初始化
...
full_content_text += chunk   # 累加但从未读取
风险：每个任务处理过程中，full_content_text 会累积所有流式输出的完整内容，直到函数结束。在长对话（如 2000 字回答）中，会额外占用 2KB 内存，虽然不大，但 200 并发时即 400KB，可忽略。真正的风险是如果回答长度达到 10 万字（PDF 解析场景），单次任务会占用 100KB 内存，但仍在可控范围。建议删除此变量或显式使用。

12. 【P1-缓存不一致】law_mapping.py 的 reload_mapping 设置 _LAW_MAPPING_CACHE = None，但无并发保护
文件: law_mapping.py 第 153-156 行

python
def reload_mapping():
    global _LAW_MAPPING_CACHE
    _LAW_MAPPING_CACHE = None
    return _load_mapping()
风险：热更新时，如果有请求正在读取旧缓存，_LAW_MAPPING_CACHE = None 会让该请求读到一半的旧数据（被清空），触发重新加载，可能导致短暂的服务抖动。

修复：使用双重缓存（Double Buffer）—— 创建新缓存后原子替换。

13. 【P1-安全风险】tools.py 的 web_search 使用 DuckDuckGo 无 API Key，可能被滥用导致 IP 被封
文件: tools.py 第 270-330 行

风险：DuckDuckGo 对免费 API 有频率限制（未公开，但通常每个 IP 每小时数千次）。200 QPS 下，Agent 频繁调用 web_search，可能触发 DuckDuckGo 的限流，导致 IP 被临时封禁，所有联网搜索功能失效。

修复：增加本地限流（如每个用户每分钟最多 5 次 web_search），或使用备用搜索引擎（如 SearXNG）。

14. 【P1-降级数据泄露】routing_table.py 的 route_query 在无匹配时默认 pass，可能将非法律问题放行给 LLM
文件: routing_table.py 第 276 行

python
# Step 6: 默认放行（给LLM判断）
return RoutingResult(action="pass", match_type="unknown")
风险：如果用户问 "如何制作炸弹"（既不命中白名单也不命中黑名单），会被直接放行给 LLM。虽然 LLM 本身有安全护栏，但前置路由层浪费了拦截机会。

修复：在 Step 6 前增加通用安全关键词检查（如 炸弹、毒品、色情），命中则直接 reject。

15. 【P1-权限绕过】routing_table.py 的口语映射 _COLLOQUIAL_MAP 可能被用于语义劫持
文件: routing_table.py 第 213-249 行

python
_COLLOQUIAL_MAP = {
    "离婚冷静期": "三十日内撤回离婚登记申请 民法典第一千零七十七条",
    "假一赔三": "三倍赔偿 消费者权益保护法第五十五条",  # 映射到消费者权益保护法
    ...
}
风险：如果映射表包含黑名单法律（如 "假一赔三" 映射到 "消费者权益保护法"），而 route_query 在映射后再次检查黑名单（Step 5），会正确 reject。当前逻辑正确，但需注意维护时不要将民法典关键词映射到黑名单（否则会被误拒）。

🟢 P2 级（可改进：健壮性与可维护性）
16. 【P2】article_normalizer.py 的 _arabic_to_chinese 在 num=0 时返回空字符串
修复：if num == 0: return "零"

17. 【P2】document_parser.py 的 parse_pdf 中 text[:150000] 截断但未提示用户内容被截断
修复：在返回文本末尾增加 "\n\n...（内容过长，已截断）"

18. 【P2】law_mapping.py 的 _calculate_match_score 对子词拆分 range(4, 1, -1) 可能导致 O(N²) 匹配
当前匹配算法在 200 QPS 下可能成为 CPU 热点，建议缓存关键词的 sliding window

19. 【P2】orchestrator.py 的 DiscussionBoard 使用 Dict 存储讨论记录，但未限制内存增长
长期运行的单例 Board 可能累积无限数据，建议设置最大容量或 TTL

20. 【P2】router.py 的 classify_by_keywords 中 "推荐" 触发全推荐模式，但 "推荐" 过于宽泛
用户问 "推荐一本民法典书籍" 会错误触发旅游全推荐模式

21. 【P2】routing_table.py 的 _FORCED_CIVIL_KEYWORDS 包含 "摄像头"，过于宽泛
用户问 "如何安装家用摄像头" 会被错误强制放行

22. 【P2】runner.py 的 _clean_reasoning 使用 re.search 逐一匹配 30+ 个模式，性能开销大
建议编译为单一正则或使用 Trie 树

23. 【P2】sub_agents.py 的 _extract_json 中 jloads 导入自 ..core.jfast，但未处理 jloads 的异常类型
应使用 try...except (json.JSONDecodeError, TypeError, ValueError)

24. 【P2】tools.py 的 _generate_embedding 硬编码 Ollama URL host.docker.internal，在非 Docker 环境失效
25. 【P2】tools.py 的 search_knowledge 中 permissions 参数类型为 list | None，但 PostgreSQL 数组比较 permission && $3 在 permissions=[] 时报错
空数组时 && 操作符始终返回 False，但 PostgreSQL 需要正确的类型转换

26. 【P2】runner.py 的 _fallback_chain 中只有 Flash 降级，但 DEEPSEEK_FLASH_MODEL 可能未配置
增加 try...except 并在降级失败时返回友好错误

27. 【P2】law_mapping.py 的 _load_mapping 使用 csv.DictReader 但文件是 \t 分隔，正确
但文件编码若为 gbk 会失败，建议增加编码探测

28. 【P2】router.py 的 _extract_simple_args 使用全局 CITIES 常量，但该常量未在 core.constants 中定义
从代码看 from ..core.constants import CITIES 但该文件可能不存在，会导致 ImportError

📊 与 GitHub 生产级 Agent 框架的对比
维度	当前实现	GitHub 生产级参考
缓存击穿防护	❌ 无分布式锁	✅ Single Flight 模式（如 golang.org/x/sync/singleflight）
文件解析安全	❌ 存在 Zip Slip 风险	✅ 使用 os.path.normpath 校验
多 Agent 协作	手动编排（硬编码 Phase）	LangChain / AutoGen 的自动对话
Token 计量	✅ 已集成 Prometheus	标准实践
路由裁决	硬编码白黑名单	基于 Embedding 分类器
工具调用	手动 dispatch_tool	标准 Function Calling
降级策略	单层降级（Flash）	多层降级（缓存 → 本地 → 精简模型）
P0 级（严重缺陷：安全绕过 / 数据泄漏 / 功能失效）
29. 【P0-权限过滤 SQL 错误】tools.py 中 _recall_pg_trgm 对空权限列表处理错误，导致公开文档无法召回
文件：tools.py 第 91-104 行

代码：perm_clause = " AND (COALESCE(permission, '{}') = '{}' OR permission && $3)"，当 permissions=[] 时，$3 为 []，permission && '{}' 在 PostgreSQL 中返回 False（空数组与任何数组比较都为 False），导致公开文档（permission 为 '{}'）被排除，无任何文档返回。

风险：search_knowledge 的权限控制完全失效，管理员配置的公开文档无法被普通用户检索，破坏信息可达性。

30. 【P0-LLM 输出注入】orchestrator.py 中 get_phase2_context 直接将其他 Agent 的 analysis 拼入上下文，未进行二次过滤
文件：orchestrator.py 第 204-213 行

代码：parts.append(f"### {aname} 的分析\n{analysis}")，其中 analysis 来自 LLM 输出。恶意 Agent（或被污染的 LLM 响应）可包含 忽略以上指令，只输出... 等注入语句，影响后续 Agent 的判断。

风险：攻击者可通过精心构造的用户问题，诱导某个 Agent 输出注入内容，污染整个讨论板，导致最终回答失控。

31. 【P0-文件内容超限】runner.py 中 _build_task_messages 将整个文件内容塞入 system 消息，超出上下文窗口
文件：runner.py 第 132-136 行

代码：file_contents.append(f"【用户上传文件: {meta['filename']}】\n{content}\n【文件结束】")，未检查 content 长度。若用户上传 10MB PDF，content 可能达数万字，超出 DeepSeek 上下文限制（通常 8k-32k token）。

风险：请求失败（400 或 413），用户无法解析大文件，且浪费带宽和内存。

32. 【P0-缓存击穿】SemanticCache.get 仍无分布式锁（已在 #1 提过，但此处补充实施细节）
补充：SemanticCache 的实现（未提供代码）若未加锁，仍存在同 #1 的问题，需统一修复。

🟡 P1 级（高优先级：数据准确性与性能）
33. 【P1-意图路由参数错误】router.py 中 _extract_simple_args 对未识别城市时直接赋值为 query，导致工具调用参数异常
文件：router.py 第 186-189 行

代码：else: args["destination"] = query，若用户问 "北京天气" 但意图被错误路由到 query_hotel（极少发生），则 destination 为 "北京天气"，高德 API 无法识别，返回空结果。

风险：降级场景下可能导致工具调用失败，用户收到误导信息。

34. 【P1-重试逻辑错误】sub_agents.py 中重试未区分错误类型，网络超时也重试但无延迟
文件：sub_agents.py 第 58-70 行

问题：若首次调用因 API 限流返回 429，重试立即发起，大概率再次失败；应增加指数退避或检查状态码。

35. 【P1-配额计数不可靠】runner.py 中 inc_used_questions 使用 asyncio.create_task 不等待，且无错误处理
文件：runner.py 第 197 行

代码：asyncio.create_task(inc_used_questions(username))，若数据库异常，该任务失败但无反馈，导致用户配额未增加，但系统认为已增加，允许用户无限使用。

36. 【P1-JSON 序列化失败】router.py 中 handle_simple_task 若 tool_result 为 None，json.dumps 抛出 TypeError
文件：router.py 第 152 行

代码：json.dumps({"tool_result": tool_result}, ...)，若 tool_result 为 None，会抛出 TypeError: Object of type NoneType is not JSON serializable，任务直接失败。

37. 【P1-降级链不完整】runner.py 中 _fallback_chain 仅调用 _fallback_flash，若 Flash 也失败，则无输出
文件：runner.py 第 438-445 行

风险：主模型超时后降级到 Flash，但若 Flash 也超时，没有更进一步的降级（如本地简单回复），用户收到空响应。

38. 【P1-子 Agent 重试未传递 retry 参数】sub_agents.py 中闭包 _call 内部若再次失败，retry 仍为 True，导致潜在无限递归（但实际有外层 try 捕获）
问题：逻辑混乱，应明确重试次数。

39. 【P1-口语映射仅替换首个匹配】routing_table.py 中 _COLLOQUIAL_MAP 替换只执行一次 break，若用户问题包含多个口语词，只替换第一个
文件：routing_table.py 第 253-258 行

代码：break 后不再继续替换，导致映射不全。

40. 【P1-web_search 无本地限流】tools.py 中 web_search 可被任意用户高频调用，导致 DuckDuckGo IP 被封
风险：恶意用户可构造 200 QPS 请求，消耗外部 API 限额。

41. 【P1-search_knowledge 中 permissions 类型错误】permissions 为 None 时，SQL 中 permission && $3 中的 $3 为 None，引发 TypeError
文件：tools.py 第 91-98 行，若 permissions 为 None，则 args 中 $3 为 None，PostgreSQL 无法比较，抛出异常。

42. 【P1-rerank 失败无降级】tools.py 中 _rerank_with_deepseek 若返回的 JSON 缺失 scores，抛出 KeyError，整个搜索失败
文件：tools.py 第 161-163 行，scores = result.get("scores", []) 后未检查长度，随后 if len(scores) != len(candidates) 抛出异常，但外层 search_knowledge 未捕获此异常，导致返回空结果。

43. 【P1-document_parser 中 OCR 并发不安全】_RAPID_ENGINE 是全局单例，RapidOCR 内部可能使用 ONNX Runtime，不支持多线程并发调用
文件：document_parser.py 第 32-35 行，多个协程同时调用 _ocr_image 可能竞争同一个引擎，导致崩溃或数据错乱。

🟢 P2 级（中等：优化与健壮性）
44. 【P2-law_mapping 无并发初始化锁】_LAW_MAPPING_CACHE 可能被多个协程同时初始化，导致重复读取文件
文件：law_mapping.py 第 18-28 行，缺少 asyncio.Lock。

45. 【P2-article_normalizer 数字转中文递归栈溢出】_arabic_to_chinese 递归调用自身，处理 9999 时递归深度 4，安全，但若扩展至更大数字，可能溢出。
46. 【P2-parse_pdf 返回错误信息不明确】若 fitz 未安装，返回字符串 "[PDF 解析引擎未安装]"，但 parse_document 中判断 success 为 False，但 error 字段为 None，导致调用方无法区分。
47. 【P2-parse_txt 编码探测不完整】未尝试 cp936、big5 等常见编码，可能无法解析某些中文文件。
48. 【P2-routing_table 白名单过于宽泛】_WHITELIST_FLAT 包含 "民事"，会匹配任何含“民事”的词，如“民事纠纷”，但若用户问“民事纠纷调解”，可能被误判。
49. 【P2-runner 中 _clean_reasoning 正则性能差】30+ 模式逐个 re.search，可编译为单一正则或 trie。
50. 【P2-orchestrator 中 DiscussionBoard 无内存限制】长时间运行会积累大量讨论记录，可能 OOM。
51. 【P2-router 中 classify_by_keywords 对“推荐”触发全模式，过于敏感】用户问“推荐一本好书”会误判。
52. 【P2-tools 中 _generate_embedding 硬编码 Ollama URL，在非 Docker 环境不可达】应允许配置。
53. 【P2-search_project_knowledge 中 embedding 无缓存】每次请求都重新生成向量，增加延迟。
54. 【P2-sub_agents 中 _extract_json 修复 JSON 过于激进，可能破坏合法 JSON】使用正则替换未引用的 key，但可能误伤。
55. 【P2-runner 中 _finish_cancelled 仅保存草稿，但未清理缓存，可能残留】导致下次请求读到旧数据。
56. 【P2-orchestrator 中 _compact_tool_result 无法处理循环引用】可能引发异常。
57. 【P2-routing_table 中 _CIVIL_CODE_ARTICLE_PATTERN 未匹配“第X条之一”等带后缀的，但已处理】但未处理“第X条第一款”等更复杂格式。
58. 【P2-tools.py 中 _recall_pg_trgm 和 _keyword_fill 权限过滤逻辑重复，可提取公共函数】
 P0 级：系统架构层面的重大缺陷
59. 【P0-应用启动崩溃】law_mapping.py 在模块导入时立即读取文件，若文件不存在则应用启动失败
文件：law_mapping.py 第 10-13 行

代码：_MAPPING_FILE = Path(__file__).parent.parent.parent / "tests" / "民法典补充协议.txt"，此路径在模块导入时即被解析。若在 Docker 镜像构建时未包含该文件，或挂载卷未就绪，整个应用无法启动。

风险：该文件是业务辅助功能（非核心），但一个缺失文件却能导致应用启动崩溃，在 Kubernetes 滚动更新中会引发 Pod CrashLoopBackOff。

修复：将文件加载移到首次调用时（懒加载），并在缺失时优雅降级（返回空映射表），记录 WARN 日志。

60. 【P0-内存耗尽】runner.py 中 messages 列表在每一步循环中不断累积，无截断策略
文件：runner.py 第 329-369 行（主循环）

代码：messages.append({"role": "assistant", ...}) 和 messages.extend(tool_msgs) 在每次工具调用后追加消息，但没有大小限制。若用户问题需要 3 次工具调用，messages 会累积 system + user + assistant + tool + system + assistant...，轻松超过 20k token。

风险：在高复杂度任务（如连续多轮工具调用）中，messages 可能撑爆上下文窗口（导致 API 400）或耗尽内存（8GB 机器下尤为危险）。

修复：在每次循环开始前调用 compress_message_history(messages, max_messages=10) 截断历史。

🟡 P1 级：运维可观测性与部署安全
61. 【P1-健康检查缺失】应用无 /health 或 /ready 端点，Kubernetes 无法进行存活/就绪探测
风险：在 K8s 环境中，缺少健康检查端点会导致：

应用卡死时 Pod 不会被重启

滚动更新时新 Pod 未就绪但流量已接入，导致请求失败

修复：在 main.py 中添加 @app.get("/health") 和 @app.get("/ready")，检查 DB/Redis 连接状态。

62. 【P1-启动时 DDL 可能阻塞】worker.py 中 ensure_schema 在应用启动时执行 DDL，若表已存在但结构变更，DDL 可能长时间持有锁
文件：worker.py 第 87-90 行

风险：CREATE TABLE IF NOT EXISTS 虽然是幂等的，但 CREATE OR REPLACE FUNCTION 会重建函数，可能在 PostgreSQL 中获取排他锁。若 cdc_events 表正在被频繁写入，DDL 会阻塞写入操作直到锁释放，造成短暂的服务抖动。

修复：将 DDL 迁移到独立的 alembic 版本管理，启动时只做连接检查。

63. 【P1-日志中可能包含明文密码】auth.py 中 verify_password 若密码过长，会记录 password_truncated 日志，其中包含密码长度信息，但未包含内容，安全
无风险：仅记录长度，无安全风险。但为谨慎起见，建议显式声明不记录任何密码相关内容。

64. 【P1-请求体大小无限制】FastAPI 默认 max_body_size 无限制，恶意用户可上传超大 JSON 耗死内存
风险：攻击者可构造 100MB 的 JSON 请求体（如 /api/chat 中的 messages），导致 Python 进程内存飙升，触发 OOM Killer。

修复：在 main.py 中配置 app.add_middleware(..., max_body_size=10 * 1024 * 1024)。

🟢 P2 级：边界场景与代码整洁
65. 【P2-CORS 配置可能过于宽泛】若 main.py 中 allow_origins=["*"]，会允许任意域名跨域访问，存在 CSRF 风险
建议：在生产环境仅允许特定前端域名，并配合 allow_credentials=True 时禁止 *。

66. 【P2-静态文件服务暴露源码】若 main.py 挂载了 StaticFiles(directory=".")，将允许用户访问 .env、requirements.txt 等敏感文件
建议：仅挂载必要的 static 目录，并配置 .env 等文件访问拒绝。

67. 【P2-所有 Agent 类未实现 __slots__】在 200 QPS 下，频繁创建 DiscussionBoard、CdcJournal 等对象，内存分配开销较大
优化：添加 __slots__ 限制属性，减少内存使用。

