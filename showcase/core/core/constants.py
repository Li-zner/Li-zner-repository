"""
共享常量模块
避免在多处硬编码重复数据（如城市列表）
"""
import functools
import json
from pathlib import Path
from typing import List

# ============================================================
# 中国主要城市列表（用于意图分类、城市提取等）
# ============================================================
CITIES: List[str] = [
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

    with open(path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    _PERSONA_CACHE[persona_id] = data
    return data


def get_all_persona_ids() -> List[str]:
    """扫描 prompts 目录获取所有人格 ID"""
    if not _PERSONA_DIR.exists():
        return []
    return [p.stem for p in _PERSONA_DIR.glob("*.json")]


def clear_persona_cache():
    """清空人格缓存（用于热加载）"""
    _PERSONA_CACHE.clear()
