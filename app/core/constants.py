"""
共享常量模块
避免在多处硬编码重复数据（如城市列表）
"""
import functools
import json
import logging
from pathlib import Path
from typing import List

# ============================================================
# 中国主要城市列表（用于意图分类、城市提取等）
# ============================================================
_BUILTIN_CITIES: List[str] = [
    "北京", "上海", "广州", "深圳", "杭州", "成都", "重庆", "武汉",
    "西安", "南京", "苏州", "天津", "长沙", "郑州", "东莞", "青岛",
    "沈阳", "宁波", "昆明", "大连", "厦门", "合肥", "佛山", "福州",
    "哈尔滨", "济南", "温州", "长春", "石家庄", "常州", "泉州",
    "南宁", "贵阳", "南昌", "太原", "烟台", "嘉兴", "南通", "金华",
    "珠海", "惠州", "徐州", "海口", "乌鲁木齐", "绍兴", "中山",
    "兰州", "保定", "潍坊", "台州", "洛阳", "岳阳", "芜湖", "柳州",
    "桂林", "三亚", "丽江", "大理", "拉萨", "呼和浩特", "银川",
    "西宁", "秦皇岛", "威海", "曲阜", "黄山", "张家界", "武夷山",
]

# 城市列表外部化（P2 #2：便于维护；缺失/损坏时回退内置）
_CITIES_FILE = Path(__file__).parent.parent.parent / "data" / "cities.json"


def _load_cities() -> List[str]:
    """加载城市列表（外部 JSON，缺失/损坏回退内置）"""
    try:
        if _CITIES_FILE.exists():
            with open(_CITIES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list) and data:
                return [str(c) for c in data]
    except Exception as e:
        logging.warning(f"城市列表加载失败，使用内置: {e}")
    return list(_BUILTIN_CITIES)


CITIES: List[str] = _load_cities()

# ============================================================
# 人格 Prompt 缓存（避免每次请求都读磁盘）
# ============================================================
_PERSONA_CACHE: dict = {}
_PERSONA_DIR = Path(__file__).parent.parent.parent / "prompts"


def get_persona_prompt(persona_id: str) -> dict:
    """获取已解析的人格配置（带缓存）"""
    if persona_id in _PERSONA_CACHE:
        return _PERSONA_CACHE[persona_id]

    path = _PERSONA_DIR / f"{persona_id}.json"
    if not path.exists():
        return {}

    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except UnicodeDecodeError:
        # GBK 编码回退（P1 #84：兼容旧版 GBK 人格配置文件）
        with open(path, "r", encoding="gbk") as f:
            data = json.load(f)
    _PERSONA_CACHE[persona_id] = data
    return data


def get_all_persona_ids() -> List[str]:
    """扫描 prompts 目录获取所有人格 ID"""
    if not _PERSONA_DIR.exists():
        logging.warning(f"prompts 目录不存在: {_PERSONA_DIR}")  # P1 #85 缺失告警
        return []
    return [p.stem for p in _PERSONA_DIR.glob("*.json")]


def clear_persona_cache():
    """清空人格缓存（用于热加载）"""
    _PERSONA_CACHE.clear()


def today_cn() -> str:
    """中文当前日期（含星期），供各处 system prompt 的 {today} 注入统一使用。
    strftime %A 会输出英文星期，中文助手场景统一为周X。

    固定用 UTC+8（北京时间）：线上容器是 UTC 时区，datetime.now() 在北京时间
    0-8 点间会注入昨天的日期（2026-09-10 线上实测：模型自称"今天是9月9日"）；
    显式时区后与容器部署在哪个时区无关。修复 修复日志 P2 时区漂移挂账项。
    """
    from datetime import datetime, timezone, timedelta
    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    now = datetime.now(timezone(timedelta(hours=8)))
    return f"{now.year}年{now.month:02d}月{now.day:02d}日 {weekdays[now.weekday()]}"
