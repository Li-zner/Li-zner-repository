"""
工具定义模块
将 OpenAI Function Calling 格式的工具定义抽离到独立模块，
便于维护、复用，避免路由文件臃肿。
"""

# ============================================================
# DeepSeek/OpenAI Function Calling 工具定义
# ============================================================

TOOL_WEATHER = {
    "type": "function",
    "function": {
        "name": "query_weather",
        "description": "查询指定城市的实时天气和未来三天预报",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "城市名，如'北京'"}
            },
            "required": ["city"]
        }
    }
}

TOOL_HOTEL = {
    "type": "function",
    "function": {
        "name": "query_hotel",
        "description": "根据目的地和预算推荐酒店",
        "parameters": {
            "type": "object",
            "properties": {
                "destination": {"type": "string", "description": "目的地城市"},
                "budget": {"type": "string", "description": "预算范围，如'经济型'或'豪华'"}
            },
            "required": ["destination"]
        }
    }
}

TOOL_ROUTE = {
    "type": "function",
    "function": {
        "name": "query_route",
        "description": "规划从A到B的交通路线",
        "parameters": {
            "type": "object",
            "properties": {
                "departure": {"type": "string", "description": "出发地"},
                "destination": {"type": "string", "description": "目的地"}
            },
            "required": ["departure", "destination"]
        }
    }
}

TOOL_FOOD = {
    "type": "function",
    "function": {
        "name": "query_food",
        "description": "推荐目的地的特色美食或餐厅",
        "parameters": {
            "type": "object",
            "properties": {
                "destination": {"type": "string", "description": "目的地城市"},
                "cuisine": {"type": "string", "description": "菜系偏好（可选）"}
            },
            "required": ["destination"]
        }
    }
}

# ============================================================
# 知识库搜索工具
# ============================================================

TOOL_SEARCH_KNOWLEDGE = {
    "type": "function",
    "function": {
        "name": "search_knowledge",
        "description": "搜索民法典知识库，查找相关法条和法律解释。当用户咨询法律问题时必须调用此工具",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词或问题描述"}
            },
            "required": ["query"]
        }
    }
}

# ============================================================
# 联网搜索工具
# ============================================================

TOOL_WEB_SEARCH = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "联网搜索实时信息。仅用于旅行场景的餐厅营业时间、排队情况、近期评价、交通预订、当前活动等实时数据，不得用于法律知识或不确定信息的补全。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词，建议包含城市/地点+查询内容，如'广州 陶陶居 营业时间 评价'"}
            },
            "required": ["query"]
        }
    }
}


# ============================================================
# 工具列表（按需组合）
# ============================================================
ALL_TOOLS = [
    TOOL_WEATHER,
    TOOL_HOTEL,
    TOOL_ROUTE,
    TOOL_FOOD,
    TOOL_SEARCH_KNOWLEDGE,
    TOOL_WEB_SEARCH,
]

TRAVEL_TOOLS = [TOOL_WEATHER, TOOL_HOTEL, TOOL_ROUTE, TOOL_FOOD]

CIVIL_CODE_TOOLS = [TOOL_SEARCH_KNOWLEDGE]

FULL_TOOLS = ALL_TOOLS  # 所有工具（用于复杂任务/推荐模式）
