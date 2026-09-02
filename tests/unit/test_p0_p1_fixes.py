"""P0/P1 修复回归测试（2026-09-02）：

1. release_concurrent 原子递减下限归零（P0 #41，杜绝并发释放打成负数）
2. claim_idempotency SET NX 原子占位（P1 #42，杜绝重复建任务）
3. CDC _renew_lock 返回值被消费（P1 #43，锁丢失立即停止消费）
4. compress_old_conversations DELETE+INSERT 同事务（P0 #44）
"""
import asyncio
from datetime import datetime, timedelta

from fake_redis import FakeRedis
from app.middleware import rate_limit
from app.core import task_manager as tm
from app.cdc.worker import LOCK_KEY, LOCK_TTL
from app.core import db_maintenance


def test_release_concurrent_never_negative():
    fake = FakeRedis()

    async def _get():
        return fake
    rate_limit.get_redis = _get

    async def _run():
        # 占位 2 个槽位后连续释放两次：1 → 0（删除 key），绝不变负
        fake._data["concurrent:u1"] = "2"
        await rate_limit.release_concurrent("u1")
        assert int(fake._data.get("concurrent:u1", 0)) == 1
        await rate_limit.release_concurrent("u1")
        assert "concurrent:u1" not in fake._data   # v<=1 → DEL，零值不残留
        # 空释放（key 不存在）也不抛异常、不创建幽灵 key
        await rate_limit.release_concurrent("ghost")
        assert "concurrent:ghost" not in fake._data

    asyncio.run(_run())


def test_claim_idempotency_dedup():
    fake = FakeRedis()
    tm._redis = _stub_redis(fake)

    async def _run():
        # 首次：占位成功 → None（调用方继续建任务）
        assert await tm.claim_idempotency("s1", "hello") is None
        assert fake._data[tm._IDEMPOTENT_PREFIX + tm._idempotent_key("s1", "hello")] == tm._CREATING
        # get_task 桩前置：claim 复用前会校验任务状态（pending/generating 才复用）
        tm.get_task = _stub_get_task({"status": "pending"})
        # 并发重复：占位期间读取为 __creating__ → 等待窗口内建方写入 task_id → 复用
        async def _late_save():
            await asyncio.sleep(0.1)
            await tm._persist_idempotent(tm._idempotent_key("s1", "hello"), "task_abc")
        asyncio.get_running_loop().create_task(_late_save())
        got = await tm.claim_idempotency("s1", "hello")
        assert got == "task_abc"
        # 旧任务已完结 → 放行新建（返回 None）
        tm.get_task = _stub_get_task({"status": "completed"})
        fake._data[tm._IDEMPOTENT_PREFIX + tm._idempotent_key("s1", "hello")] = "task_old"
        assert await tm.claim_idempotency("s1", "hello") is None
        # Redis 异常容错：返回 None 不抛
        async def _boom():
            raise RuntimeError("down")
        tm._redis = _stub_redis(None, raise_exc=_boom)
        assert await tm.claim_idempotency("s1", "hello") is None

    asyncio.run(_run())


def test_renew_lock_result_honored():
    fake = FakeRedis()
    fake._data[LOCK_KEY] = "token_a"

    class W:
        from app.cdc.worker import CdcWorker
        _renew_lock = CdcWorker._renew_lock

    async def _run():
        w = W()
        assert await w._renew_lock(fake, "token_a") is True    # 持有者续期成功
        assert await w._renew_lock(fake, "token_b") is False   # 他人 token → 拒绝

    asyncio.run(_run())


def test_compress_uses_transaction():
    executed, tx_log = [], []

    class FakeTx:
        async def __aenter__(self):
            tx_log.append("begin")
            return self
        async def __aexit__(self, et, ev, tb):
            tx_log.append("rollback" if et else "commit")
            return False

    class FakeConn:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        def transaction(self):
            return FakeTx()
        async def fetch(self, sql, *a):
            if "GROUP BY" in sql:
                return [{"user_id": "u1", "conversation_id": "c1",
                         "msg_count": 2, "last_msg": datetime.now()}]
            return [
                {"role": "user", "content": "问1", "created_at": datetime.now() - timedelta(days=40)},
                {"role": "assistant", "content": "答1", "created_at": datetime.now() - timedelta(days=39)},
            ]
        async def execute(self, sql, *a):
            executed.append("DELETE" if "DELETE" in sql else "INSERT")

    class FakePool:
        def acquire(self):
            # asyncpg pool.acquire 返回异步上下文管理器（非协程）
            return FakeConn()

    db_maintenance.get_pool = _stub_pool(FakePool())

    async def _run():
        await db_maintenance.compress_old_conversations()

    asyncio.run(_run())
    # 关键断言：DELETE 与 INSERT 被同一事务包裹，且顺序正确
    assert tx_log == ["begin", "commit"]
    assert executed == ["DELETE", "INSERT"]


def test_recommend_tools_no_keyerror():
    from types import SimpleNamespace
    import app.services.chat_fast_paths as cf

    async def _tool(agent, *a, **k):
        return {"ok": True, "agent": agent}
    # P0 #45：原实现 as_completed 返回包装协程回查 Task 字典必 KeyError，整条推荐通道静默降级
    # 回归：结果按 (agent, result) 归位，不再依赖回查。
    cf.dispatch_tool = _tool

    ctx = SimpleNamespace(
        matched_agents=["query_weather", "query_hotel"],
        user_query="北京旅游",
        req=SimpleNamespace(user_location="北京市"),
    )
    out, tr = [], []
    async def _run():
        async for ev in cf._run_recommend_tools(ctx, tr):
            out.append(ev)
    asyncio.run(_run())

    assert {n for n, _ in tr} == {"query_weather", "query_hotel"}
    assert sum("tool_call" in e for e in out) == 2
    assert sum("tool_result" in e for e in out) == 2


def test_recommend_tools_timeout_appends_error():
    from types import SimpleNamespace
    import app.services.chat_fast_paths as cf

    async def _slow(agent, *a, **k):
        if agent == "query_hotel":
            await asyncio.sleep(1)
        return {"ok": True, "agent": agent}
    cf.dispatch_tool = _slow
    old = cf.TOOL_TIMEOUT
    cf.TOOL_TIMEOUT = 0.05

    ctx = SimpleNamespace(
        matched_agents=["query_weather", "query_hotel"],
        user_query="北京旅游",
        req=SimpleNamespace(user_location="北京市"),
    )
    out, tr = [], []
    async def _run():
        async for ev in cf._run_recommend_tools(ctx, tr):
            out.append(ev)
    try:
        asyncio.run(_run())
    finally:
        cf.TOOL_TIMEOUT = old

    names = {n for n, _ in tr}
    assert names == {"query_weather", "query_hotel"}          # 超时工具也被补入
    assert any(r.get("error") == "tool timeout" for n, r in tr)  # 超时结果归一，不抛 KeyError


def test_release_idempotency_claim_only_creating():
    fake = FakeRedis()
    tm._redis = _stub_redis(fake)

    async def _run():
        # 占位后释放，键被清掉（可立即重试）
        assert await tm.claim_idempotency("s1", "msg") is None
        await tm.release_idempotency_claim("s1", "msg")
        assert tm._IDEMPOTENT_PREFIX + tm._idempotent_key("s1", "msg") not in fake._data
        # 若已被他人写入真实 task_id，则条件删除不应误删
        k = tm._IDEMPOTENT_PREFIX + tm._idempotent_key("s1", "msg")
        fake._data[k] = "task_x"
        await tm.release_idempotency_claim("s1", "msg")
        assert fake._data[k] == "task_x"

    asyncio.run(_run())


def test_fallback_chain_guarantees_terminal_event():
    from types import SimpleNamespace
    from app.services import chat_fallback as fb
    from app.core import semantic_cache

    async def empty_flash(req, username):
        return
    async def set_empty(*a, **k):
        return True
    fb.fallback_flash = empty_flash
    semantic_cache.SemanticCache.set_empty = set_empty

    req = SimpleNamespace(query="x", lang="zh")
    async def _run():
        out = []
        async for c in fb.fallback_chain(req, "u"):   # fallback_flash 静默不产事件
            out.append(c)
        # 主模型+Flash 双双失败时也必须有终结事件（防前端一直转圈）
        assert any("answer_complete" in c for c in out)
        assert any("[DONE]" in c for c in out)
    asyncio.run(_run())


def test_half_open_failure_reopens():
    from app.middleware.circuit_breaker import SimpleBreaker
    br = SimpleBreaker(fail_threshold=3, recover_timeout=30)

    async def ok(): return "ok"
    async def boom(): raise RuntimeError("down")

    async def _run():
        # 打到 OPEN
        for _ in range(3):
            try: await br.call(boom)
            except Exception: pass
        assert br.state == "OPEN"
        # 到时间进入 HALF_OPEN
        br.last_fail_time = 0
        try:
            await br.call(boom)   # HALF_OPEN 试探失败
        except Exception:
            pass
        assert br.state == "OPEN", "HALF_OPEN 一次失败应即回 OPEN"
    asyncio.run(_run())


# ---- 桩助手 ----
def _stub_redis(fake, raise_exc=None):
    async def _get():
        if raise_exc:
            raise raise_exc()
        return fake
    return _get


def _stub_pool(pool):
    async def _get():
        return pool
    return _get


def _stub_get_task(ret):
    async def _get(task_id):
        return ret
    return _get