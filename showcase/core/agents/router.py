"""
任务路由模块 — 简单任务 vs 复杂任务的分发决策器

功能:
  1. classify_intent() - 基于关键词 + LLM 轻量分类，判断用户意图涉及的领域
  2. RouteDecision - 路由决策结果，告诉调用方是"简单"还是"复杂"
  3. handle_simple_task() - 直接执行单个 Agent + 一次 LLM 格式化，不走多 Agent 协商

设计原则:
  - 简单任务: 精确匹配关键词，直接调工具 → LLM 格式化 → 返回，零多余开销
  - 复杂任务: 走现有的四 Agent 圆桌讨论流程 (orchestrator.py)
"""

import json
import os
import asyncio
import httpx
import re
from typing import List, Dict, Optional
from ..core.logging import setup_logging
from ..core.config import DEEPSEEK_API_BASE, DEEPSEEK_MODEL, HTTP_TIMEOUT_SHORT, HTTP_TIMEOUT_MEDIUM

logger = setup_logging()

# ============================================================
# 关键词意图映射表
# ============================================================
INTENT_KEYWORDS: Dict[str, List[str]] = {
    "query_weather": ["天气", "气温", "温度", "下雨", "晴天", "刮风", "气候", "气象",
                      "天气预报", "几度", "多少度", "冷不冷", "热不热"],
    "query_hotel":   ["酒店", "住宿", "旅馆", "民宿", "客房", "房间", "留宿", "过夜",
                      "订房", "预约酒店", "宾馆", "入住"],
    "query_route":   ["路线", "怎么去", "交通", "乘车", "驾车", "公交", "地铁", "到达", "前往",
                      "开车", "打车", "坐车", "多远", "多久",
                      "坐什么车", "几路", "导航", "怎么走"],
    "query_food":    ["美食", "餐厅", "吃饭", "菜系", "小吃", "特色菜", "餐饮",
                      "馆子", "饭馆", "食堂", "好吃", "推荐菜", "吃货", "去哪吃"],
    "search_knowledge": ["民法典", "法律", "法条", "法规", "合同法", "侵权", "继承",
                         "婚姻", "物权", "债权", "诉讼时效", "民事",
                         "权利", "义务", "赔偿", "纠纷", "打官司"],
    "web_search":    [],  # 由 LLM 自行判断是否调用，不通过关键词匹配
    "search_project_knowledge": ["支付", "充值", "钱包", "扣费", "交易流水",
                                   "并发安全", "分布式锁", "乐观锁",
                                   "支付系统", "支付架构", "支付体系",
                                   "技术栈", "项目架构", "系统设计",
                                   "高并发", "压测", "QPS", "TPS",
                                   "异常处理", "对账", "回调", "库存"],
}

# 如果匹配"推荐"涵盖四个领域，则为全推荐模式
RECOMMEND_PATTERNS = [
    "推荐", "建议", "攻略", "安排", "规划行程", "旅游计划",
    "有什么好玩", "帮我规划", "行程安排", "旅行攻略"
]

# 如果匹配到多个领域的关键词，超过此数量视为复杂任务
SIMPLE_MAX_DOMAINS = 1


def classify_by_keywords(query: str) -> Dict:
    """
    基于关键词的意图分类（零 API 调用）
    返回: {"agents": [...], "is_simple": bool, "is_recommend": bool}
    """
    matched = set()
    for agent, keywords in INTENT_KEYWORDS.items():
        for kw in keywords:
            if kw in query:
                matched.add(agent)
                break  # 一个 agent 只计一次

    matched_list = sorted(matched)

    # 检测是否为全推荐模式
    # 条件：用户表达了全面的旅行规划需求
    is_recommend = False
    has_recommend_kw = any(p in query for p in RECOMMEND_PATTERNS)
    # 法律咨询场景的"推荐"不触发旅游全推荐（P2 #20：如"推荐一本民法典书籍"）
    if has_recommend_kw and any(kw in query for kw in ("民法典", "法律", "法条", "法规")):
        has_recommend_kw = False
    if has_recommend_kw:
        # 触发全推荐模式的条件（满足任意一条即可）：
        # 1. 匹配了至少2个领域关键词
        # 2. 包含明确的全局规划意图词（攻略/规划/安排/行程等）
        # 3. 匹配了1个领域且包含推荐/安排/规划等词
        comprehensive_plan = any(p in query for p in [
            "攻略", "规划", "安排", "行程", "旅游计划",
            "旅行攻略", "帮我规划", "帮我安排"
        ])
        if len(matched_list) >= 2:
            is_recommend = True
        elif comprehensive_plan:
            is_recommend = True
        elif len(matched_list) >= 1 and has_recommend_kw:
            is_recommend = True

    # 全推荐模式下，启用所有4个旅游Agent
    if is_recommend:
        for agent in ["query_weather", "query_hotel", "query_route", "query_food"]:
            matched.add(agent)
        matched_list = sorted(matched)
        # 全推荐模式永远复杂
        return {
            "agents": matched_list,
            "is_simple": False,
            "is_recommend": True,
            "method": "keyword"
        }

    # 排除 search_knowledge 的简单/复杂判断（知识库永远走单独路径）
    travel_agents = [a for a in matched_list if a != "search_knowledge"]
    has_knowledge = "search_knowledge" in matched_list

    # 如果既有知识库又有旅游意图 → 复杂
    if has_knowledge and travel_agents:
        return {
            "agents": matched_list,
            "is_simple": False,
            "is_recommend": False,
            "method": "keyword"
        }

    # 纯知识库 → 简单
    if has_knowledge and not travel_agents:
        return {
            "agents": matched_list,
            "is_simple": True,
            "is_recommend": False,
            "method": "keyword"
        }

    return {
        "agents": matched_list,
        "is_simple": len(matched_list) <= SIMPLE_MAX_DOMAINS,
        "is_recommend": False,
        "method": "keyword"
    }


async def classify_by_llm(query: str) -> Dict:
    """
    基于 LLM 的轻量意图分类（用于关键词无法判断的边界情况）
    只做分类，不生成回答，token 消耗极小。
    返回: {"agents": [...], "is_simple": bool}
    """
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        logger.warning("LLM 分类无 API Key，回退关键词分类")
        return classify_by_keywords(query)

    system_prompt = """你是一个意图分类器。分析用户问题，判断涉及以下哪些领域（可多选）：
- query_weather: 天气/气温相关
- query_hotel: 酒店/住宿相关
- query_route: 路线/交通相关
- query_food: 美食/餐厅相关
- search_knowledge: 民法典/法律相关

输出 JSON 格式：
{"agents": ["query_weather"], "is_simple": true, "is_recommend": false}

规则：
- 如果只涉及 1 个领域 → is_simple: true
- 如果涉及 2 个及以上 → is_simple: false（需要多 Agent 协作）
- 如果不涉及任何领域 → agents: [], is_simple: true
- 如果用户请求旅游推荐/攻略/安排行程 → agents 包含 query_weather,query_hotel,query_route,query_food 全部四个，is_simple: false, is_recommend: true
- 只输出 JSON，不要输出其他文字"""

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SHORT) as client:
            resp = await client.post(
                f"{DEEPSEEK_API_BASE}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": DEEPSEEK_MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": f"用户问题: {query}"}
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.0,
                    "max_tokens": 100
                }
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            result = json.loads(content)
            result["method"] = "llm"
            # 确保 is_recommend 字段存在
            if "is_recommend" not in result:
                result["is_recommend"] = False
            return result
    except Exception as e:
        logger.warning(f"LLM 分类失败，回退关键词: {e}")
        kw_result = classify_by_keywords(query)
        kw_result["method"] = "keyword_fallback"
        return kw_result


async def classify_intent(query: str, use_llm: bool = False) -> Dict:
    """
    统一的意图分类入口

    Args:
        query: 用户问题
        use_llm: 是否优先使用 LLM 分类（更准确但慢一点）

    返回:
        {"agents": [...], "is_simple": bool, "is_recommend": bool, "method": str}
    """
    if use_llm:
        return await classify_by_llm(query)
    return classify_by_keywords(query)


def get_tools_for_intent(agents: List[str]) -> list:
    """
    根据意图匹配的 Agent 列表，返回对应的工具定义列表。
    只包含匹配到的工具，不包含全部工具 → 减小 prompt 长度，加快 LLM 响应。

    返回: tools list (function calling 格式)
    """
    from .tool_definitions import (
        TOOL_WEATHER, TOOL_HOTEL, TOOL_ROUTE, TOOL_FOOD,
        TOOL_SEARCH_KNOWLEDGE, TOOL_WEB_SEARCH,
        TOOL_SEARCH_PROJECT_KNOWLEDGE,
    )
    TOOL_MAP = {
        "query_weather": TOOL_WEATHER,
        "query_hotel": TOOL_HOTEL,
        "query_route": TOOL_ROUTE,
        "query_food": TOOL_FOOD,
        "search_knowledge": TOOL_SEARCH_KNOWLEDGE,
        "web_search": TOOL_WEB_SEARCH,
        "search_project_knowledge": TOOL_SEARCH_PROJECT_KNOWLEDGE,
    }
    tools = []
    # 始终包含 web_search + project_knowledge 工具（由 LLM 自行判断是否使用）
    tools.append(TOOL_WEB_SEARCH)
    if TOOL_SEARCH_PROJECT_KNOWLEDGE not in tools:
        tools.append(TOOL_SEARCH_PROJECT_KNOWLEDGE)
    for agent in agents:
        tool = TOOL_MAP.get(agent)
        if tool and tool not in tools:
            tools.append(tool)
    return tools


def get_agent_names_for_orchestrator(agents: List[str]) -> List[str]:
    """
    返回应该参与多 Agent 讨论的 Agent 名称列表。
    排除 search_knowledge（知识库不需要参与圆桌讨论）。
    """
    return [a for a in agents if a in ("query_weather", "query_hotel", "query_route", "query_food")]


# ============================================================
# 简单任务执行器
# ============================================================

async def handle_simple_task(
    task_id: str,
    username: str,
    session_id: str,
    user_query: str,
    agent_name: str,
    user_location: str = "",
    persona_id: str = "",
    file_ids: Optional[List[str]] = None,
):
    """
    简单任务处理 — 不走四 Agent 圆桌讨论

    流程:
      1. 调用对应工具（单次调用，不流式）
      2. 将结果发给 LLM 格式化回答（单次调用，不流式）
      3. 结果写入 Redis

    适用于: 查询天气、查酒店、查路线、查美食等单一领域问题
    """
    from ..core.redis import get_redis
    from ..core.task_manager import update_status, append_result, read_accumulated_result, cleanup_event
    from ..core.memory_manager import MemoryManager
    from ..core.semantic_cache import SemanticCache
    from ..core.metrics import llm_tokens_total, llm_tokens_detail, llm_requests_total
    from .tools import fetch_weather_async
    from .sub_agents import call_sub_agent

    conv_id = session_id or f"conv_{username}_{int(time.time())}"
    mm = MemoryManager(username, conv_id)
    full_text = ""
    final_answer = ""

    try:
        await update_status(task_id, "generating")

        # ---- 1. 调用单个工具 ----
        tool_result = None
        if agent_name == "query_weather":
            tool_result = await fetch_weather_async(user_query)
        elif agent_name in ("query_hotel", "query_route", "query_food"):
            # 简单任务：从关键词中提取城市/目的地参数
            args = _extract_simple_args(agent_name, user_query, user_location)
            tool_result = await call_sub_agent(agent_name, args, user_query)
        else:
            tool_result = {"error": f"未知工具: {agent_name}"}

        if tool_result is None:
            tool_result = {"error": "工具返回空"}

        # 工具结果写入进度
        await append_result(task_id, json.dumps({"tool_result": tool_result}, ensure_ascii=False) + "\n")

        # ---- 2. 轻量 LLM 格式化回答（单次调用）----
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            await update_status(task_id, "error", "DEEPSEEK_API_KEY 未设置")
            return

        # 构建简单 prompt
        prompt = _build_simple_prompt(user_query, agent_name, tool_result, user_location, persona_id)

        try:
            from ..core.config import DEEPSEEK_MODEL
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_MEDIUM) as client:
                resp = await client.post(
                    f"{DEEPSEEK_API_BASE}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={
                        "model": DEEPSEEK_MODEL,
                        "messages": [
                            {"role": "system", "content": prompt["system"]},
                            {"role": "user", "content": prompt["user"]}
                        ],
                        "temperature": 0.3,
                        "max_tokens": 1024
                    }
                )
                resp.raise_for_status()
                data = resp.json()
                final_answer = data["choices"][0]["message"]["content"]

                # Token 计量
                usage = data.get("usage", {})
                pt = usage.get("prompt_tokens", 0) or 0
                ct = usage.get("completion_tokens", 0) or 0
                if pt or ct:
                    llm_tokens_total.labels(type='input').inc(pt)
                    llm_tokens_total.labels(type='output').inc(ct)
                    llm_tokens_detail.labels(model=DEEPSEEK_MODEL, endpoint='simple_task', type='input').inc(pt)
                    llm_tokens_detail.labels(model=DEEPSEEK_MODEL, endpoint='simple_task', type='output').inc(ct)
                llm_requests_total.labels(model=DEEPSEEK_MODEL, endpoint='simple_task', status='success').inc()
        except Exception as e:
            logger.warning(f"简单任务 LLM 格式化失败: {e}")
            # 降级：直接返回工具结果
            final_answer = json.dumps(tool_result, ensure_ascii=False)

        # ---- 3. 写入结果 ----
        # DFA 检查
        from ..core.safety_filter import get_filter
        sf = get_filter()
        if sf.contains_sensitive(final_answer):
            final_answer = sf.safe_message

        # 分块写入 Redis
        for i in range(0, len(final_answer), 80):
            chunk = final_answer[i:i+80]
            await append_result(task_id, chunk)

        await update_status(task_id, "completed", final_answer)

        # 保存记忆
        await mm.save_messages(
            {"role": "user", "content": user_query},
            {"role": "assistant", "content": final_answer}
        )
        asyncio.create_task(SemanticCache.set(user_query, final_answer))
        logger.info(f"✅ 简单任务完成: task_id={task_id}, agent={agent_name}")

    except Exception as e:
        logger.error(f"简单任务异常: {e}", exc_info=True)
        await update_status(task_id, "error", str(e))
    finally:
        cleanup_event(task_id)


def _extract_simple_args(agent_name: str, query: str, user_location: str = "") -> dict:
    """
    从查询中提取简单参数（关键词匹配，无需 LLM）
    用于简单任务的工具调用
    """
    args = {}

    if agent_name == "query_hotel":
        # 提取目的地城市
        city = _extract_city(query)
        if city:
            args["destination"] = city
        elif user_location:
            args["destination"] = user_location.replace("市", "")
        else:
            args["destination"] = query
        # 提取预算关键词
        budget_kw = ["经济", "便宜", "实惠", "预算", "省钱"]
        luxury_kw = ["豪华", "五星", "高档", "贵", "奢侈"]
        if any(kw in query for kw in budget_kw):
            args["budget"] = "经济型"
        elif any(kw in query for kw in luxury_kw):
            args["budget"] = "豪华"

    elif agent_name == "query_route":
        # 简单提取：如果用户说"从A到B"
        from_match = re.search(r'从(.+?)到(.+?)(?:怎么|的|$|，)', query)
        if from_match:
            args["departure"] = from_match.group(1).strip()
            args["destination"] = from_match.group(2).strip()
        else:
            # 默认当前位置到某地
            args["departure"] = user_location or "当前位置"
            dest = _extract_city(query)
            args["destination"] = dest or query

    elif agent_name == "query_food":
        city = _extract_city(query)
        if city:
            args["destination"] = city
        elif user_location:
            args["destination"] = user_location.replace("市", "")
        else:
            args["destination"] = query
        # 提取菜系
        cuisine_kw = ["川菜", "粤菜", "湘菜", "火锅", "烧烤", "日料", "西餐", "中餐"]
        for c in cuisine_kw:
            if c in query:
                args["cuisine"] = c
                break

    return args


def _extract_city(text: str) -> Optional[str]:
    """从文本中提取城市名（简单关键词匹配）"""
    from ..core.constants import CITIES
    for c in CITIES:
        if c in text:
            return c
    return None


def _build_simple_prompt(query: str, agent_name: str, tool_result: dict,
                         user_location: str = "", persona_id: str = "") -> dict:
    """
    构建简单任务的 system + user prompt
    不走多 Agent 讨论，直接让 LLM 基于工具结果回答问题
    """
    from ..core.persona_manager import get_persona_manager
    pm = get_persona_manager()
    persona = pm.current

    today_str = __import__('datetime').datetime.now().strftime("%Y年%m月%d日 %A")
    try:
        system = persona.system_prompt.format(today=today_str, name=persona.name) if persona else f"你是AI助手。今天是{today_str}。"
    except (KeyError, ValueError):
        # 人格提示词缺占位符时不崩溃（与 v2 的 _safe_format_prompt 一致）
        system = persona.system_prompt if persona else f"你是AI助手。今天是{today_str}。"

    # 注入定位
    if user_location:
        system += f"\n用户当前所在城市：{user_location}。"

    # 注入工具结果
    system += (
        f"\n\n你使用工具 [{agent_name}] 查询到了以下结果。\n\n"
        f"## 🚨 核心规则（必须遵守）\n"
        f"1. **禁止编造**：只能基于工具返回的数据回答，不能编造任何具体数据（价格、距离、气温、评分等）\n"
        f"2. **先推荐再追问**：如果用户是求推荐，第一句就直接给推荐方案，不要反问\n"
        f"3. **数据不足时**：明确告诉用户哪些信息是工具提供的，哪些是估算的\n\n"
        f"工具返回的数据：\n{json.dumps(tool_result, ensure_ascii=False, indent=2)}"
    )

    return {
        "system": system,
        "user": query
    }
