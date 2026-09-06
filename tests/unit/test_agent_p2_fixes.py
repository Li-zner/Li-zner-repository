"""app/agents P2 专项修复回归测试（2026-09-05 第二轮，见 app/agents/修复日志.md）：

1. web_search 限流计数字典惰性清扫（防每用户一键无界增长）
2. generate_summary 空摘要与异常同样指数退避重试
3. 重建锁续期协程：不再持有即停止（防长任务锁过期后并发重复重建）
4. route_query 拆分后行为等价（灰区放行/口语映射回填/强制白名单/普通白名单）
"""
import asyncio
import time

import pytest

import app.agents.tools as agent_tools
from app.agents import routing_table
from app.agents.memory import generate_summary
from app.agents.runner import _renew_until_done


# ============================================================
# 1. web_search 限流：键数超阈值触发惰性清扫
# ============================================================
def test_search_rate_state_swept_when_overflow():
    async def _run():
        now = time.time()
        # 预置大量已过窗口的旧键，模拟长期运行后的每用户残留
        for i in range(600):
            agent_tools._search_rate_state[f"expired_{i}"] = (now - 3600, 9)
        for i in range(3):
            await agent_tools._check_search_rate(f"fresh_{i}")
        return dict(agent_tools._search_rate_state)

    try:
        state = asyncio.run(_run())
        fresh_left = [k for k in state if k.startswith("fresh_")]
        assert len(state) <= 20, "超阈值后必须惰性清扫过期键，字典不能无界增长"
        assert len(fresh_left) == 3, "活跃用户（窗口未过期）不能被误清"
    finally:
        agent_tools._search_rate_state.clear()


def test_search_rate_limit_still_blocks():
    """清扫不能破坏限流本身：同一键窗口内超上限必须抛出"""
    async def _run():
        agent_tools._search_rate_state.clear()
        try:
            for _ in range(10):
                await agent_tools._check_search_rate("user_x")
            with pytest.raises(RuntimeError):
                await agent_tools._check_search_rate("user_x")
        finally:
            agent_tools._search_rate_state.clear()

    asyncio.run(_run())


# ============================================================
# 2. generate_summary：空摘要与异常同样退避重试
# ============================================================
class _FakeResp:
    def __init__(self, content):
        self._content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


class _FakeClient:
    """替换 httpx.AsyncClient：记录调用次数，返回预设内容"""

    content = "   "

    def __init__(self, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *a, **kw):
        _FakeClient.calls += 1
        return _FakeResp(_FakeClient.content)


def test_generate_summary_retries_empty_with_backoff(monkeypatch):
    import app.agents.memory as amem
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    _FakeClient.calls = 0
    _FakeClient.content = "   "  # 空摘要 → 必须重试
    sleeps = []

    async def _fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(amem.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(amem.asyncio, "sleep", _fake_sleep)

    got = asyncio.run(generate_summary([{"role": "user", "content": "帮我总结这次对话"}]))
    assert got == ""
    assert _FakeClient.calls == 3, "空摘要必须重试满 3 次"
    assert sleeps == [0.5, 1.0], "空摘要与异常同样指数退避，不能立即重打"


def test_generate_summary_success_first_try(monkeypatch):
    import app.agents.memory as amem
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    _FakeClient.calls = 0
    _FakeClient.content = " 用户想要规划行程 "
    sleeps = []

    async def _fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(amem.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(amem.asyncio, "sleep", _fake_sleep)

    got = asyncio.run(generate_summary([{"role": "user", "content": "帮我总结这次对话"}]))
    assert got == "用户想要规划行程"
    assert _FakeClient.calls == 1
    assert sleeps == [], "首次成功不应退避"


# ============================================================
# 3. 重建锁续期：不再持有即停止（不死循环）
# ============================================================
def test_renew_until_done_stops_when_not_holder(monkeypatch):
    import app.agents.runner as runner_mod
    calls = {"n": 0}

    class _FakeCache:
        @staticmethod
        async def renew_rebuild_lock(query, token, cache_ctx="", ttl=45):
            calls["n"] += 1
            return calls["n"] < 2  # 第一次续期成功，第二次返回 False（已不持有）

    monkeypatch.setattr(runner_mod, "SemanticCache", _FakeCache)

    async def _instant_sleep(_seconds):
        return  # 空 awaitable：注意不能再调 asyncio.sleep（已被 patch，会无限递归）

    monkeypatch.setattr(runner_mod.asyncio, "sleep", _instant_sleep)

    async def _run():
        await asyncio.wait_for(_renew_until_done("q", "tok", "ctx"), timeout=5)

    asyncio.run(_run())
    assert calls["n"] == 2, "续期返回 False 后必须停止，不能死循环"


# ============================================================
# 4. route_query 拆分后行为等价（避开安全拦截路径，另测常规裁决）
# ============================================================
def test_route_query_gray_zone_pass():
    r = routing_table.route_query("被网络诈骗了怎么办")
    assert r.action == "pass"
    assert r.match_type.startswith("gray_zone_"), "刑事灰区问题必须放行给 LLM 做混合回答"


def test_route_query_colloquial_mapping_propagated():
    r = routing_table.route_query("想离婚怎么办")
    assert r.action == "pass"
    assert r.mapped_query, "命中口语映射时必须带回 mapped_query 供检索使用"
    assert "离婚请求权" in r.mapped_query


def test_route_query_forced_and_plain_whitelist():
    r1 = routing_table.route_query("未成年人打赏主播的钱能退吗")
    assert r1.action == "pass" and r1.match_type == "forced_whitelist"
    r2 = routing_table.route_query("邻居装修噪音太大扰民")
    assert r2.action == "pass" and r2.match_type == "whitelist"


def test_route_query_empty():
    assert routing_table.route_query("").action == "pass"
