"""2026-09-05/06 审查修复回归测试（routes/services/core 三轮 + agent 流程复审，见各目录修复日志.md）

1. P0：oauth callback 路由必须绑定 auth_github_callback（装饰器曾挂错到
   _resolve_github_user，GitHub 登录整条链路断裂）
2. P1：chat_react / react_steps 工具参数非法 JSON 时按占位错误处理，
   tool_results 与 tool_calls 等长对齐（下轮 LLM 不再 400 / 任务不再 TypeError）
3. P1：compress_message_history 切点不拆散 assistant(tool_calls)/tool 消息配对
"""
from fastapi import FastAPI


def test_oauth_callback_route_binds_to_real_handler():
    """P0 回归：callback 路径必须绑定 auth_github_callback，
    _resolve_github_user 只能是普通 helper 不得占用路由"""
    import app.routes.oauth as oauth_mod

    app = FastAPI()
    app.include_router(oauth_mod.router)
    cb = [r for r in app.routes if getattr(r, "path", "") == "/auth/github/callback"]
    assert len(cb) == 1, f"callback 路由应恰好注册一次，实际 {len(cb)}"
    assert cb[0].name == "auth_github_callback", \
        f"callback 路由必须绑定 auth_github_callback，实际绑定 {cb[0].name}"
    assert any(getattr(r, "path", "") == "/auth/github" for r in app.routes), \
        "GitHub 授权跳转路由必须存在"


# ============================================================
# 流式路径（chat_react）：坏参数占位 + 结果对齐
# ============================================================
def test_chat_react_bad_tool_args_keep_alignment():
    """P1 回归（2026-09-06）：坏参数与好参数混合时 tool_results 必须与 tool_calls
    等长且按下标对齐——坏调用得 {error} 占位，好调用结果不错位。
    旧实现 continue 跳过坏参数导致错位 + 末尾 tool_call 缺 tool 消息（下轮 LLM 400）。
    """
    import asyncio
    from app.services import chat_react as cr

    class _Ctx:
        req = type("R", (), {"query": "q", "user_location": ""})()
        user_perms = []
        username = "u1"

    dispatched = []

    async def _fake_dispatch(name, args, q, perms, username):
        dispatched.append((name, args))
        return {"ok": True, "name": name}

    cr.dispatch_tool = _fake_dispatch  # 打桩：只验证占位与对齐接线
    ctx = _Ctx()
    tool_calls = [
        {"id": "call_1", "type": "function", "function": {"name": "query_weather", "arguments": "不是JSON"}},
        {"id": "call_2", "type": "function", "function": {"name": "query_food", "arguments": '{"destination": "成都"}'}},
    ]

    async def _collect():
        frame = {}
        out = []
        async for ev in cr._execute_react_tools(ctx, tool_calls, frame):
            out.append(ev)
        return out, frame

    events, frame = asyncio.run(_collect())
    results = frame["tool_results"]
    assert len(results) == len(tool_calls), "结果数必须与调用数一致（否则下轮 LLM 400）"
    assert isinstance(results[0], dict) and "error" in results[0], "坏参数必须得占位错误结果"
    assert results[1].get("name") == "query_food", "好调用的结果不得错位"
    assert dispatched == [("query_food", {"destination": "成都"})], "坏参数不得传入 dispatch_tool"


# ============================================================
# 任务路径（react_steps）：坏参数占位必须是协程，gather 不炸 + user_location 透传
# ============================================================
def test_react_steps_bad_tool_args_no_typeerror():
    """P1 回归（2026-09-06）：_tool_arg_error 必须是 async（返回协程），
    否则同步 dict 混进 asyncio.gather 当场 TypeError 炸整个任务。"""
    import asyncio
    from app.agents import react_steps as rs

    import inspect
    assert inspect.iscoroutinefunction(rs._tool_arg_error), "占位函数必须是协程"

    dispatched = []

    async def _fake_dispatch(name, args, q, perms, username):
        dispatched.append((name, username))
        return {"ok": True}

    async def _fake_discussion(*a, **k):
        return ""

    rs.dispatch_tool = _fake_dispatch
    rs._run_discussion = _fake_discussion
    tool_calls = [
        {"id": "c1", "type": "function", "function": {"name": "query_weather", "arguments": "{bad"}},
        {"id": "c2", "type": "function", "function": {"name": "query_food", "arguments": '{"destination": "x"}'}},
    ]
    messages: list = []

    async def _run():
        return await rs._handle_tool_step(
            "t1", "u1", "q", "", messages, tool_calls, {}, [])

    branch = asyncio.run(_run())
    assert branch == "ok"
    assert dispatched == [("query_food", "u1")], "坏参数不得进 dispatch，username 必须透传"
    # 回填的 tool 消息必须与 tool_calls 一一配对（每个 tool_call_id 都有 tool 消息）
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["c1", "c2"]


# ============================================================
# 历史压缩：切点不拆散 assistant(tool_calls)/tool 配对
# ============================================================
def test_compress_keeps_tool_pairing():
    """P1 回归（2026-09-06）：压缩切点落在 assistant(tool_calls) 与 tool 消息之间时，
    必须向左对齐切点，输出中不得出现孤儿 tool 消息（否则下一次 LLM 请求 400）。"""
    from app.agents.memory import compress_message_history

    msgs = [{"role": "system", "content": "sys"}]
    for i in range(5):
        msgs.append({"role": "user", "content": f"问题{i}"})
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}"}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": f"结果{i}"})
    # 追加一组不完整的 u+a，使默认切点恰好落在"问题3"组的 assistant/tool 之间
    msgs.append({"role": "user", "content": "问题5"})
    msgs.append({"role": "assistant", "content": "回答5", "tool_calls": [{"id": "c5"}]})
    # 18 条 > max_messages=6，切分后 recent 以孤儿 tool 开头（修复前）
    out = compress_message_history(msgs, max_messages=6)
    assert len(out) < len(msgs), "压缩必须实际发生（否则用例没踩中切分路径）"
    for i, m in enumerate(out):
        if m.get("role") == "tool":
            prev = out[i - 1] if i else None
            assert prev is not None and prev.get("role") == "assistant" and prev.get("tool_calls"), \
                f"index {i} 出现孤儿 tool 消息（切点拆散了配对）"
