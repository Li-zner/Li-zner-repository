"""place_extract 回归检查：出发地/目的地提取语义（2026-09"广州/成都"泄漏 bug）

出发地手动输入功能已移除：出发地只认自然语言"从X"，无定位兜底。

运行：python tests/test_place_extract.py（纯函数，无外部依赖，退出码 0=全过）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.place_extract import (  # noqa: E402
    auto_tool_args, extract_departure, extract_destination,
)


def check(name, actual, expected):
    assert actual == expected, f"[FAIL] {name}: got {actual!r}, want {expected!r}"
    print(f"[ok] {name}: {actual!r}")


def main():
    # 核心场景（线上 bug）：问题问成都，工具必须查成都而非出发地
    check("目的地提取", extract_destination("从广州出发，想去成都玩5天"), "成都")
    check("weather 查目的地", auto_tool_args("query_weather", "从广州出发，想去成都玩5天"),
          {"city": "成都"})
    check("hotel 查目的地", auto_tool_args("query_hotel", "从广州出发，想去成都玩5天"),
          {"destination": "成都"})
    check("food 查目的地", auto_tool_args("query_food", "从广州出发，想去成都玩5天"),
          {"destination": "成都"})
    check("route 出发地+目的地", auto_tool_args("query_route", "从广州出发，想去成都玩5天"),
          {"departure": "广州", "destination": "成都"})

    # "从A到B"整句（router 原有能力保留）
    check("从A到B正则", auto_tool_args("query_route", "从广州到成都怎么走"),
          {"departure": "广州", "destination": "成都"})

    # 只提目的地，无"从X" → 出发地为空，route 由子代理按"当前位置"处理
    check("无从X则无出发地", extract_departure("成都5日游攻略"), "")
    check("目的地正常提取", extract_destination("成都5日游攻略"), "成都")
    check("目的地带去字", extract_destination("去成都的攻略"), "成都")
    check("route 默认当前位置", auto_tool_args("query_route", "去成都玩"),
          {"departure": "当前位置", "destination": "成都"})

    # 只提出发地/无城市
    check("只提出发地", extract_destination("广州有什么好玩的"), "广州")
    check("无城市时目的地兜底原话", auto_tool_args("query_weather", "今天天气怎么样"),
          {"city": "今天天气怎么样"})

    # 别名归一
    check("别名蓉城", extract_destination("想去蓉城吃火锅"), "成都")

    # 出发地被"从"标记时，标志词不把它当地目的地
    check("出发地即目的地", extract_destination("从广州出发去哪都行，先查广州天气"), "广州")

    print("\n全部通过")


if __name__ == "__main__":
    main()
