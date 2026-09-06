"""app/core P1 修复回归测试（2026-09-04）：

1. DFA 白名单同句豁免：白名单词只豁免所在句，跨句敏感词仍拦截（修复全文一票豁免绕过）
2. PersonaManager.get_effective：按请求解析人格且不改全局 current（修复并发串人格）
"""
from app.core.safety_filter import SafetyFilter
from app.core.persona_manager import PersonaManager


def _sf() -> SafetyFilter:
    return SafetyFilter()


def test_same_sentence_whitelist_exempts():
    """敏感词与白名单词同句（法律讲解场景）→ 豁免"""
    sf = _sf()
    assert sf.contains_sensitive("根据民法典的规定，诈骗罪的认定需要主观故意。") is False


def test_cross_sentence_whitelist_no_longer_bypasses():
    """白名单词在另一句 → 不再豁免本句敏感词（旧实现全文一票豁免，此用例回归）"""
    sf = _sf()
    text = "今天带大家了解民法典。接下来教你制作炸弹的步骤。"
    assert sf.contains_sensitive(text) is True


def test_sensitive_without_whitelist_still_blocked():
    sf = _sf()
    assert sf.contains_sensitive("教你制作炸弹的步骤") is True
    assert sf.contains_sensitive("聊聊钓鱼和赌博的技巧") is True


def test_check_stream_same_sentence_rule():
    """流式检查与整段检查规则一致"""
    sf = _sf()
    ok = sf.check_stream("根据民法典讲解诈骗罪的构成要件")
    assert ok["safe"] is True
    bad = sf.check_stream("民法典先讲到这里。教你制作炸弹需要准备")
    assert bad["safe"] is False
    # chunk = 命中词之前的安全前缀（炸弹之前的内容全部放行）
    assert bad["chunk"] == "民法典先讲到这里。教你制作"
    assert bad["triggered_word"] == "炸弹"


def test_get_effective_resolves_explicit_persona():
    """显式 persona_id 优先返回对应人格"""
    pm = PersonaManager()
    if "civil_code" not in pm._personas:
        return  # 环境无人格文件时跳过（降级单例只有 unified）
    p = pm.get_effective("civil_code")
    assert p is not None and p.id == "civil_code"


def test_get_effective_does_not_mutate_global():
    """get_effective 只读：current 不被并发请求改写（修复全局串人格）"""
    pm = PersonaManager()
    before = pm.current_id
    if len(pm._personas) < 2:
        return  # 单人格环境无从验证切换，跳过
    other = next(pid for pid in pm._personas if pid != before)
    p = pm.get_effective(other)
    assert p.id == other
    assert pm.current_id == before  # 全局默认未被修改


def test_get_effective_falls_back_to_current():
    pm = PersonaManager()
    cur = pm.current
    assert pm.get_effective(None) is cur
    assert pm.get_effective("") is cur
    assert pm.get_effective("__not_exist__") is cur


def test_inc_used_questions_invalidates_user_cache():
    """used_requests 实际自增时必须失效 auth 用户信息缓存（修复 5 分钟窗口内额度绕过）"""
    import asyncio
    from app.core import quota
    from fake_redis import FakeRedis

    fake = FakeRedis()
    fake._data["user:info:u1"] = '{"used_requests": 5}'

    class _Conn:
        async def execute(self, *a, **k):
            return status
    class _CM:
        def __init__(self, c): self._c = c
        async def __aenter__(self): return self._c
        async def __aexit__(self, *a): return False
    class _Pool:
        def acquire(self): return _CM(_Conn())

    async def _fake_get_pool():
        return _Pool()
    async def _fake_get_redis():
        return fake
    quota.get_pool = _fake_get_pool      # inc_used_questions 内 await get_pool()（模块顶层导入名）
    quota.get_redis = _fake_get_redis

    # UPDATE 1：实际自增 → 缓存必须被删
    status = "UPDATE 1"
    asyncio.run(quota.inc_used_questions("u1"))
    assert "user:info:u1" not in fake._data, "自增后未失效用户缓存"

    # UPDATE 0：非受限用户 no-op → 不应触发删除（user:info:u2 不存在，写探针验证）
    fake._data["user:info:u2"] = "keep"
    status = "UPDATE 0"
    asyncio.run(quota.inc_used_questions("u2"))
    assert fake._data["user:info:u2"] == "keep"


def test_refresh_rotation_replay_rejected():
    """同一 refresh token 第二次兑换必须 401（NX 原子抢占修复并发重放窗口）"""
    import asyncio
    from app.middleware import auth
    from fake_redis import FakeRedis

    fake = FakeRedis()
    async def _gr(): return fake
    auth.get_redis = _gr
    async def _gu(username):
        return {"username": username, "role": "user", "is_active": True}
    auth.get_user = _gu

    refresh = auth.create_refresh_token({"sub": "u1"})
    first = asyncio.run(auth.refresh_access_token(refresh))
    assert first["access_token"] and first["refresh_token"]

    try:
        asyncio.run(auth.refresh_access_token(refresh))
        assert False, "重放的 refresh token 未被拒绝"
    except Exception as e:
        assert getattr(e, "status_code", None) == 401


def test_half_open_single_probe():
    """HALF_OPEN 只放一个试探请求，其余秒拒；试探成功回 CLOSED（标准单探针语义）"""
    import asyncio
    from app.middleware.circuit_breaker import SimpleBreaker, CircuitBreakerError

    async def _run():
        b = SimpleBreaker(fail_threshold=1, recover_timeout=0.05, call_timeout=5)
        async def fail():
            raise RuntimeError("boom")
        try:
            await b.call(fail)
        except RuntimeError:
            pass
        assert b.state == "OPEN"
        await asyncio.sleep(0.06)

        entered = asyncio.Event()
        async def probe():
            entered.set()
            await asyncio.sleep(0.2)
            return "ok"
        t1 = asyncio.create_task(b.call(probe))
        await entered.wait()  # t1 已转入 HALF_OPEN
        try:
            await b.call(probe)
            assert False, "试探期间的并发请求未被秒拒"
        except CircuitBreakerError:
            pass
        assert await t1 == "ok"
        assert b.state == "CLOSED"

    asyncio.run(_run())


def test_task_ownership_access_control():
    """任务端点归属校验：非 owner 一律 404（不泄露存在性），admin 豁免，缺 owner 拒绝"""
    from fastapi import HTTPException
    from app.services.agent_tasks import ensure_task_access

    task = {"owner": "alice"}
    ensure_task_access(task, "alice", "user")          # 本人 → 通过
    ensure_task_access(task, "bob", "admin")           # admin → 通过
    try:
        ensure_task_access(task, "mallory", "user")
        assert False, "越权访问未被拒绝"
    except HTTPException as e:
        assert e.status_code == 404                     # 404 而非 403，不泄露存在性
    try:
        ensure_task_access({}, "bob", "user")          # 缺 owner（旧任务）→ 拒绝
        assert False, "无 owner 任务未被拒绝"
    except HTTPException as e:
        assert e.status_code == 404


def test_create_task_stores_owner():
    """create_task 落 owner 字段（FakeRedis 后端）"""
    import asyncio
    from app.core import task_manager as tm
    from fake_redis import FakeRedis

    class _HashFake(FakeRedis):
        """补齐 task_manager 需要的 hash 语义（局部，不动共享 FakeRedis）"""
        async def hset(self, key, mapping=None):
            self._data.setdefault(key, {}).update(mapping or {})
        async def hgetall(self, key):
            return dict(self._data.get(key, {}))
        async def expire(self, key, ttl):
            return True
        async def append(self, key, chunk):
            self._data[key] = self._data.get(key, "") + chunk

    fake = _HashFake()
    async def _gr(): return fake
    tm._get_redis = _gr

    async def _run():
        tid = await tm.create_task("conv1", "hello", owner="alice")
        return await tm.get_task(tid)
    task = asyncio.run(_run())
    assert task["owner"] == "alice"
