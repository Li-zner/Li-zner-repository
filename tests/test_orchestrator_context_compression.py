import os

os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

from app.agents.orchestrator import DiscussionBoard, build_shared_context


def test_phase1_context_is_compacted_and_shared():
    board = DiscussionBoard(
        "我想去北京玩三天，预算 5000，带我妈一起",
        build_shared_context(
            "我想去北京玩三天，预算 5000，带我妈一起",
            user_location="上海",
            user_profile="最近城市: 上海 | 预算: 5000",
        ),
    )
    board.add_tool_result(
        "query_weather",
        {"city": "北京", "weather": "晴", "temperature": 28, "winddirection": "北风"},
    )
    board.add_tool_result(
        "query_hotel",
        {"hotels": [{"name": "北京中信大酒店"}]},
    )

    context = board.get_phase1_context()

    assert "共享背景" in context
    assert "上海" in context
    assert "预算 5000" in context
    assert "北京天气: 晴, 28°C" in context
    assert "找到 1 家酒店" in context
    assert "temperature" not in context
    assert "winddirection" not in context
