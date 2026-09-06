"""出发地/目的地提取（纯函数、仅依赖 constants，可独立单测）

2026-09 修复"广州/成都"泄漏 bug 的公共层：此前三处各自实现的工具参数提取
（stream_utils._auto_args / chat_fast_paths.build_simple_tool_args /
router._extract_simple_args）存在同一组语义错误：
  - 侧栏"出发地"user_location 被当成 hotel/food 的目的地（用户填广州，问成都，查的却是广州）
  - weather 按城市表顺序扫描，"从广州去成都"先命中表中靠前的广州
  - route 的 destination 被塞入整句用户原话，子代理拿到的参数是垃圾
模型拿到自相矛盾的工具数据后把内部推理整段漏进正文。

语义（2026-09 定稿，出发地手动输入功能已整体移除）：
  - 出发地 = 问题中显式"从X"；route 无显式出发地时由子代理按"当前位置"处理
  - 目的地 = 问题中"去/到/前往/飞X"的城市 > 其他城市候选 > 原话兜底
"""
import re
from .constants import CITIES

# 口语别名归一（原 stream_utils._CITY_ALIASES 同源）
CITY_ALIASES = {
    "帝都": "北京", "魔都": "上海", "羊城": "广州", "鹏城": "深圳",
    "蓉城": "成都", "山城": "重庆", "春城": "昆明", "冰城": "哈尔滨",
    "泉城": "济南", "榕城": "福州", "星城": "长沙", "江城": "武汉",
}

# 目的地标志词。不含"在"——"我在广州"是位置陈述而非目的地意图
_DEST_MARKERS = ("去", "到", "前往", "飞")

# "从A到B"整句路线（支持城市表之外的地名，如"从天河到机场"）
_FROM_TO_RE = re.compile(r"从(.{2,12}?)到(.{2,12}?)(?:[，。,.\s]|怎么|的|$)")


def strip_shi(name: str) -> str:
    """剥"市"后缀（高德等接口与展示都用裸城市名）"""
    return (name or "").replace("市", "")


def query_cities(user_query: str) -> list:
    """问题中出现的城市候选（别名归一后保序去重）"""
    found: list = []
    for alias, city in CITY_ALIASES.items():
        if alias in user_query and city not in found:
            found.append(city)
    for c in CITIES:
        if c in user_query and c not in found:
            found.append(c)
    return found


def extract_departure(user_query: str) -> str:
    """出发地：仅认问题中显式"从X"（无定位输入，不再兜底）"""
    for c in query_cities(user_query):
        if f"从{c}" in user_query:
            return c
    return ""


def extract_destination(user_query: str) -> str:
    """目的地：标志词城市（跳过被"从"标记的出发地）> 其他候选 > 唯一候选兜底 > 空串

    最后一级回退到唯一候选，覆盖"先查广州天气"这类唯一城市恰为出发地的问法。
    """
    cands = query_cities(user_query)
    dep = extract_departure(user_query)
    for c in cands:
        if c == dep and f"从{c}" in user_query:
            continue
        if any(m + c in user_query for m in _DEST_MARKERS):
            return c
    for c in cands:
        if c != dep:
            return c
    return cands[0] if cands else ""


def auto_tool_args(name: str, user_query: str) -> dict:
    """按工具名自动补全参数（fast_paths / dispatch_tool / router 三处共用）

    仅处理旅游四工具；其余工具返回空 dict（查询类参数由调用方自带）。
    无城市可提取时以原话兜底（子代理/接口自行失败处理）。
    """
    if name == "query_weather":
        return {"city": extract_destination(user_query) or user_query}
    if name in ("query_hotel", "query_food"):
        return {"destination": extract_destination(user_query) or user_query}
    if name == "query_route":
        m = _FROM_TO_RE.search(user_query)
        if m:
            dep, dest = strip_shi(m.group(1)), strip_shi(m.group(2))
        else:
            dep = extract_departure(user_query)
            dest = extract_destination(user_query)
        return {
            "departure": dep or "当前位置",
            "destination": dest or user_query,
        }
    return {}
