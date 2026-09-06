"""app/agents 本次修复的回归测试（2026-09-05）：

1. document_parser 的 Zip Slip：raw 成员名含 '..' 必须被剔除（不能用 normpath，它会折叠 '..'）
2. orchestrator.run_phase1：分析失败（error:True）的 Agent 不得当作有效意见进摘要/Phase2
3. runner 防击穿：等待超时仍未拿到重建锁时降级完成，而非未持锁重建（防缓存雪崩）
4. law_mapping 默认匹配阈值：提高至 0.5，降低误纠正
5. routing_table 强制白名单："未成年人"过宽改具体组合（未成年人打赏/充值），防误路由
6. tools web_search 限流：改全局限流为按用户隔离
"""
import asyncio
import zipfile
import io

from app.agents import document_parser as dp
from app.agents.orchestrator import AgentOrchestrator


# ---- 1) Zip Slip：raw 成员名判断 ----
def test_docx_media_zip_slip_blocked():
    """word/media/../X 这类穿越成员必须被剔除；合法成员保留。"""
    # 复现 document_parser 的过滤逻辑：RAW 名切分判断，非 normpath
    def _filter(namelist):
        return [
            f for f in namelist
            if f.startswith('word/media/')
            and not f.startswith('/')
            and '..' not in f.split('/')
        ]

    legit = ["word/media/image1.png", "word/media/a/image2.jpeg"]
    evil = ["word/media/../document.xml", "word/media/../../etc/passwd", "/word/media/abs.png"]
    assert _filter(legit) == legit
    assert _filter(evil) == []


def test_zip_slip_filter_matches_parser_comprehension():
    """确认代码里真正用的过滤表达式 == 测试里的（防修了测试没修代码）。"""
    src = open(dp.__file__, encoding="utf-8").read()
    assert "and '..' not in f.split('/')" in src, "parser 应使用 raw 成员名切分判断 '..'"
    assert "normpath(f).split(os.sep)" not in src, "parser 不应再用 normpath（会被折叠绕过）"


# ---- 2) orchestrator：失败 Agent 不进意见/摘要 ----
async def _run_phase1_with_failure(monkeypatch):
    """query_weather 失败、query_hotel 正常；以 monkeypatch 替换 _call_deepseek_think。"""
    import app.agents.orchestrator as orch_mod

    async def fake_think(system_prompt, context, temperature=0.3, agent_id="unknown", phase="think"):
        if agent_id == "query_weather":
            return {"analysis": "（分析失败: JSON解析错误）", "cross_comments": "无", "suggestions": [], "error": True}
        return {"analysis": "市中心酒店A不错，建议住那里", "cross_comments": "无", "suggestions": ["住A"]}

    monkeypatch.setattr(orch_mod, "_call_deepseek_think", fake_think)
    orch = AgentOrchestrator("北京旅游")  # 2026-09 refactor: user_context 参数已移除
    orch.add_tool_result("query_weather", {"temperature": "20"})
    orch.add_tool_result("query_hotel", {"hotels": []})
    opinions = await orch.run_phase1()
    return opinions, orch.board.get_discussion_summary(), orch.board.errors


def test_failed_agent_not_treated_as_opinion(monkeypatch):
    opinions, summary, errors = asyncio.run(_run_phase1_with_failure(monkeypatch))
    assert "query_weather" not in opinions, "失败 Agent 不得作为有效意见"
    assert "query_hotel" in opinions, "正常 Agent 必须保留"
    assert "分析失败" not in summary, "摘要不得混入错误文本"
    assert "酒店A不错" in summary, "正常分析应出现在摘要"
    assert errors.get("query_weather"), "失败 Agent 应被标记未响应"


# ---- 3) runner：等待超时未拿锁 → 降级完成，不重建 ----
def test_runner_lock_timeout_degrades(monkeypatch):
    import app.agents.runner as r
    import app.core.semantic_cache as sc

    calls = {"acquire": 0}
    statuses = []
    cancelled = asyncio.Event()

    async def fake_acquire_lock(q, cache_ctx="", ttl=45):
        calls["acquire"] += 1
        return None  # 永远拿不到锁

    async def fake_cache_hit(task_id, username, user_query, mm, cache_ctx=""):
        return False  # 等待期间也读不到缓存

    async def fake_update_status(task_id, status, content=""):
        statuses.append((status, content))
        return None

    # 用 monkeypatch 挂到真实类上，避免污染其它测试（test_rebuild_lock 依赖真实现）
    monkeypatch.setattr(sc.SemanticCache, "acquire_rebuild_lock", fake_acquire_lock)
    monkeypatch.setattr(r, "_try_cache_hit", fake_cache_hit)
    monkeypatch.setattr(r, "update_status", fake_update_status)
    monkeypatch.setattr(r, "cleanup_event", lambda task_id: None)
    monkeypatch.setattr(r, "is_cancelled", lambda task_id: False)
    monkeypatch.setattr(r, "read_accumulated_result", lambda task_id: "")
    monkeypatch.setattr(r, "_finish_cancelled", lambda task_id, partial: None)
    monkeypatch.setattr(r, "get_cancel_event", lambda task_id: asyncio.Event())
    monkeypatch.setattr(r, "MemoryManager", lambda *a, **k: None)
    monkeypatch.setattr(r, "_safe_inc_used_questions", lambda username: _noop())

    asyncio.run(r.run_agent_task("t1", "u1", "s1", "北京天气"))

    # 获取锁 >= 2 次（首取 + 重试），且最终降级为 completed，而不是继续重建写缓存
    assert calls["acquire"] >= 2, calls
    assert any(s == "completed" for s, _ in statuses), statuses


async def _noop():
    return None


# ---- 4) law_mapping 默认阈值 0.5 ----
def test_law_mapping_default_threshold_raised():
    import inspect
    from app.agents import law_mapping as lm
    sig = inspect.signature(lm.check_query)
    assert sig.parameters["threshold"].default == 0.5, "默认阈值应提高至 0.5 防误纠正"


# ---- 5) routing_table：未成年人强制白名单改具体组合 ----
def test_minors_forced_keyword_narrowed():
    from app.agents.routing_table import _FORCED_CIVIL_KEYWORDS
    assert "未成年人" not in _FORCED_CIVIL_KEYWORDS, "裸'未成年人'过于宽泛，应移除"
    # 具体组合仍保留强制放行能力
    assert "未成年人打赏" in _FORCED_CIVIL_KEYWORDS
    assert "未成年人充值" in _FORCED_CIVIL_KEYWORDS


def test_minors_protection_question_not_forced_civil():
    from app.agents.routing_table import route_query
    # "未成年人保护法如何规定"主要属《未成年人保护法》，不应因裸"未成年人"被强制放行进民法典
    r = route_query("未成年人保护法如何规定")
    assert r.match_type != "forced_whitelist", f"不应被强制白名单放行: {r.match_type}"


# ---- 6) tools web_search 按用户限流 ----
def test_web_search_passes_user_key(monkeypatch):
    """web_search 必须把 user_key 透传给 _check_search_rate（按用户隔离）。"""
    import app.agents.tools as tools

    captured = {}

    async def fake_check(user_key=""):
        captured["user_key"] = user_key

    async def fake_instant(query):
        return {"results": [], "total": 0}

    monkeypatch.setattr(tools, "_check_search_rate", fake_check)
    monkeypatch.setattr(tools, "_instant_answer", fake_instant)

    asyncio.run(tools.web_search("test query", user_key="alice"))
    assert captured.get("user_key") == "alice", captured


def test_search_rate_isolation_per_user(monkeypatch):
    """不同 user_key 各自计数互不影响；同 key 达到上限即限流。"""
    import app.agents.tools as tools

    async def limit_10(user_key=""):
        for _ in range(_SEARCH_MAX):
            await tools._check_search_rate(user_key)

    monkeypatch.setattr(tools, "_search_rate_state", {})
    _SEARCH_MAX = tools._SEARCH_MAX_PER_WINDOW

    async def run():
        await limit_10("alice")
        # alice 已满 10 次
        try:
            await tools._check_search_rate("alice")
            alice_blocked = False
        except RuntimeError:
            alice_blocked = True
        # bob 不受影响
        await tools._check_search_rate("bob")
        return alice_blocked

    assert asyncio.run(run()) is True, "同 user_key 达上限应限流"
