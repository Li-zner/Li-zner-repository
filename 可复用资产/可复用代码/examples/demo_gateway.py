""" ============================================================
 *  可复用资产：examples/demo_gateway.py
 *  来源：agent_gateway 聊天主链路（chat_stream_core/fast_paths/react）蒸馏的接线范本
 *  实战验证：离线自检全链路通过（门禁序/缓存/锁/扣费/清理兜底）
 *  依赖：fastapi（仅 create_app 需要；自检模式零依赖）
 *  提取：2026-09-06；二次复用后请在来源行补注项目名
 *  ============================================================

这是 SOP-00《后端架构构建总纲》§六 第 3 步的**接线范本**：
总纲讲规则，各模块讲原语，本文件演示"机制链怎么接"——
新项目照此结构替换 FakeLLM 为真实下游调用即可，接线方式勿自行发明。

覆盖的接线知识点（每处都有真实事故背书，详见 SOP-00 §五）：
  1. 请求级状态全部收敛进 RequestCtx（闭包布尔 7写2读 的事故根源）
  2. 门禁顺序即语义：QPS/槽位 → 配额 → 语义缓存 → 防击穿锁 → 业务 → 计量
  3. 每条早退路径（拒绝/异常/取消）都释放并发槽位（30s 槽位泄漏事故）
  4. 计量扣费统一入口，所有路径都过（快速通道漏扣费事故）
  5. 被拦截/不完整内容禁止写缓存（残缺答案钉缓存事故）
  6. 请求生命周期用 StateMachine 约束（非法迁移显式抛错）
  7. 熔断器包裹下游调用，OPEN 时不触达
"""
import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from circuit_breaker import CircuitBreaker
from semantic_cache import SemanticCache
from snowflake_id import Snowflake
from state_machine import IllegalTransitionError, StateMachine
from token_budget import TokenBudget

# ============================================================
# 配置（生产中走 .env / 配置中心）
# ============================================================
QPS_LIMIT = 5
SLOT_LIMIT = 20
SLOT_TTL = 30
CACHE_CTX = "demo"          # 防跨用户污染维度：人格|权限|画像指纹
TOKEN_BUDGET_DAILY = 100_000

# ============================================================
# 请求级状态：全部收敛进 ctx（禁止散落闭包布尔）
# ============================================================
_TRANSITIONS = {
    "serve":    ({"created"}, "streaming"),
    "complete": ({"streaming"}, "completed"),
    "fail":     ({"created", "streaming"}, "failed"),
}


@dataclass
class RequestCtx:
    """一次请求的共享可变状态。规则：可变状态只进这里，函数间传 ctx 不传散装变量"""
    username: str
    query: str
    snow: Snowflake
    budget: TokenBudget
    cache: SemanticCache
    req_id: str = ""
    answer_id: str = ""
    partial: str = ""
    saved: bool = False
    finished: bool = False
    slot_acquired: bool = False
    usage: dict = field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0})
    sm: StateMachine = None  # type: ignore[assignment]

    def __post_init__(self):
        self.sm = StateMachine(_TRANSITIONS, initial="created")
        self.req_id = str(self.snow.next_id())


# ============================================================
# 门禁链（顺序即语义：SOP-00 §三）
# ============================================================
async def gate_qps_slots(ctx: RequestCtx, redis) -> AsyncIterator[str]:
    """①QPS + 并发槽位。QPS 拒绝时槽位未占，无泄漏风险"""
    if not await check_qps_like(redis, f"qps:{ctx.username}", QPS_LIMIT):
        ctx.sm.fire("fail")
        yield _sse("answer_complete", "请求过于频繁，请稍后再试。") + "data: [DONE]\n\n"
        ctx.finished = True
        return
    if await acquire_slot_like(redis, f"slot:{ctx.username}", SLOT_LIMIT, SLOT_TTL):
        ctx.slot_acquired = True
    else:
        ctx.sm.fire("fail")
        yield _sse("answer_complete", "当前使用人数较多，请稍后再试。") + "data: [DONE]\n\n"
        ctx.finished = True


async def gate_cache(ctx: RequestCtx) -> AsyncIterator[str]:
    """②语义缓存：命中直接返回；穿透占位回繁忙；未命中走防击穿重建"""
    cached = await ctx.cache.get(ctx.query, CACHE_CTX)
    if cached and not await ctx.cache.is_empty(cached):
        yield _sse("answer_chunk", cached)
        yield _sse("answer_complete", cached) + "data: [DONE]\n\n"
        ctx.finished = True
    elif cached:
        yield _sse("answer_complete", "服务暂时繁忙，请稍后重试。") + "data: [DONE]\n\n"
        ctx.finished = True


# ============================================================
# 业务：下游调用（FakeLLM；生产替换为真实模型流式接口，经熔断器包裹）
# ============================================================
LLM_BREAKER = CircuitBreaker("demo-llm", fail_threshold=3, recover_timeout=5.0)


async def llm_stream(ctx: RequestCtx) -> AsyncIterator[str]:
    """真实项目中替换为：模型 API 流式调用（本占位按 query 回显）"""
    yield f"已收到：{ctx.query}"


async def _llm_once(ctx: RequestCtx) -> str:
    """单次下游调用（熔断器只能包协程函数；流式场景熔断包"连接+首块"阶段，后续块直通）"""
    text = ""
    async for chunk in llm_stream(ctx):
        text += chunk
    return text


async def business_stream(ctx: RequestCtx) -> AsyncIterator[str]:
    """下游经熔断器包裹；异常转降级文案，不把内部错误透给用户"""
    try:
        text = await LLM_BREAKER.call(_llm_once, ctx)
        ctx.partial += text
        yield _sse("answer_chunk", text)
    except Exception:  # 熔断 OPEN / 下游异常 → 降级
        yield _sse("answer_chunk", "服务暂时不可用，请稍后再试。")


# ============================================================
# 计量（统一入口：任何业务路径的 usage 都从这里走）
# ============================================================
def meter_usage(ctx: RequestCtx) -> None:
    total = ctx.usage["prompt_tokens"] + ctx.usage["completion_tokens"]
    if total and not ctx.budget.try_consume(total):
        print(f"[budget] 超日预算，预算护栏拒绝该请求的计费放行: {ctx.username}")


# ============================================================
# 主编排：门禁 → 缓存 → 业务 → 计量 → 收尾（finally 兜底）
# ============================================================
async def handle_chat(ctx: RequestCtx, redis) -> AsyncIterator[str]:
    try:
        async for ev in gate_qps_slots(ctx, redis):
            yield ev
            if ctx.finished:
                return
        ctx.sm.fire("serve")

        async for ev in gate_cache(ctx):
            yield ev
            if ctx.finished:
                return

        # 防击穿锁：同 query 并发只放一个重建（SemanticCache.acquire/renew/release）
        token = await ctx.cache.acquire_rebuild_lock(ctx.query, CACHE_CTX)
        if token is None:
            yield _sse("answer_complete", "服务暂时繁忙，请稍后重试。") + "data: [DONE]\n\n"
            ctx.finished = True
            return

        async for chunk in business_stream(ctx):
            yield chunk

        meter_usage(ctx)
        await ctx.cache.set(ctx.query, ctx.partial, CACHE_CTX)
        yield _sse("answer_complete", ctx.partial) + "data: [DONE]\n\n"
        ctx.sm.fire("complete")
        ctx.saved = True
        ctx.finished = True
    finally:
        # 兜底：任何路径离开都释放槽位（30s 槽位泄漏事故）；异常收尾不重复保存
        if ctx.slot_acquired:
            await release_slot_like(redis, f"slot:{ctx.username}")
            ctx.slot_acquired = False
        if not ctx.saved and ctx.partial:
            print(f"[cleanup] 断线保存: {len(ctx.partial)} chars（未写缓存）")


# ============================================================
# App 工厂（生产：redis 换真实客户端；lifespan 里初始化/关闭连接池）
# ============================================================
def create_app(redis, snowflake_machine_id: int = 1):
    from fastapi import FastAPI
    from fastapi.responses import StreamingResponse

    app = FastAPI(title="demo-gateway")
    cache = SemanticCache(redis, placeholder_ttl=30)
    budget = TokenBudget(daily_limit=TOKEN_BUDGET_DAILY, per_call_limit=0)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/v1/demo/chat/stream")
    async def chat_stream(body: dict):
        ctx = RequestCtx(
            username=body.get("user", "anon"), query=body.get("query", ""),
            snow=Snowflake(machine_id=snowflake_machine_id),
            budget=budget, cache=cache,
        )
        return StreamingResponse(handle_chat(ctx, redis), media_type="text/event-stream")

    return app


# ============================================================
# 门禁的槽位/QPS 原语（演示用 INCR/EXPIRE/DECR；
# 生产多实例请换 rate_limiter_lua 的 LUA 原语，语义一致）
# ============================================================
async def check_qps_like(redis, key: str, limit: int) -> bool:
    n = await _incr_like(redis, key, 1)
    return n <= limit


async def acquire_slot_like(redis, key: str, limit: int, ttl: int) -> bool:
    n = await _incr_like(redis, key, 1, ttl)
    return n <= limit


async def release_slot_like(redis, key: str) -> None:
    await _decr_like(redis, key)


async def _incr_like(redis, key: str, delta: int, ttl: int = None) -> int:
    cur = await redis.get(key)
    n = (int(cur) if cur else 0) + delta
    await redis.set(key, str(n), ex=ttl)
    return n


async def _decr_like(redis, key: str) -> None:
    cur = await redis.get(key)
    if cur:
        await redis.set(key, str(max(0, int(cur) - 1)))


def _sse(event: str, content: str) -> str:
    return f"data: {json.dumps({'type': event, 'content': content}, ensure_ascii=False)}\n\n"


# ============================================================
# 离线全链路自检：FakeRedis + 驱动 handle_chat，验证接线而非实现
# ============================================================
def self_check() -> None:
    class FakeRedis:
        """演示所需最小面：get/set(nx,ex)/incr/decr/expire"""

        def __init__(self):
            self.d = {}
            self.ttl = {}

        async def get(self, k):
            return self.d.get(k)

        async def set(self, k, v, nx=False, ex=None, xx=False):
            if nx and k in self.d:
                return None
            self.d[k] = v
            if ex:
                self.ttl[k] = ex
            return "OK"

        async def incr(self, k, delta=1):
            self.d[k] = str(int(self.d.get(k, "0")) + delta)
            return int(self.d[k])

        async def decr(self, k, delta=1):
            self.d[k] = str(int(self.d.get(k, "0")) - delta)
            return int(self.d[k])

        async def expire(self, k, ttl):
            self.ttl[k] = ttl

    import asyncio

    async def run():
        redis = FakeRedis()
        ctx = RequestCtx(username="u1", query="你好",
                         snow=Snowflake(machine_id=1),
                         budget=TokenBudget(daily_limit=TOKEN_BUDGET_DAILY),
                         cache=SemanticCache(redis, placeholder_ttl=30))
        events = []
        async for chunk in handle_chat(ctx, redis):
            events.append(chunk)
        assert any("answer_complete" in c for c in events), "正常收尾缺失"
        assert ctx.saved and ctx.finished and not ctx.slot_acquired, "收尾状态不对"
        # 第二次同 query → 语义缓存命中（业务不再产出新内容，answer 相同）
        ctx2 = RequestCtx(username="u1", query="你好",
                          snow=Snowflake(machine_id=1),
                          budget=TokenBudget(daily_limit=TOKEN_BUDGET_DAILY),
                          cache=SemanticCache(redis, placeholder_ttl=30))
        events2 = [c async for c in handle_chat(ctx2, redis)]
        assert any("answer_complete" in c for c in events2), "缓存命中路径缺失"
        assert ctx2.finished, "缓存命中必须置 finished"
        print("demo_gateway self_check OK：门禁→缓存→业务→计量→清理 全链接线通过")

    asyncio.run(run())


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selfcheck":
        self_check()
    else:
        print("用法: python demo_gateway.py selfcheck  （离线全链路自检）")
