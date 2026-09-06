"""支付充值失败路径回归测试（2026-09-05）

buggy：process_recharge 在渠道失败分支（channel_result.success=False）只把订单标记为
failed，却仍在函数末尾引用只在成功分支赋值的 now/after_balance → UnboundLocalError → HTTP 500。
修复：将事务内结算抽成 _settle_recharge，失败分支返回 (None, None)，外层安全组装失败响应。
"""
import asyncio

import app.payment.service as svc


# ---- 最小桩：连接/事务/连接池 ----
class _Tx:
    def __init__(self, conn):
        self._conn = conn
    async def __aenter__(self):
        return self
    async def __aexit__(self, et, ev, tb):
        return False


class _Conn:
    def transaction(self):
        return _Tx(self)
    async def fetchrow(self, sql, *a):
        # FOR UPDATE 锁行读（失败路径无需 wallet，成功路径不在此桩覆盖）
        return {"order_no": "PA20260000000001", "status": "pending",
                "amount": 100, "payment_method": "simulated_alipay", "expire_at": None}
    async def execute(self, sql, *a):
        return "UPDATE 1"


class _Pool:
    """最小连接池：async with pool.acquire(timeout=..) as conn 返回 _Conn。"""
    def __init__(self, conn):
        self._conn = conn
    def acquire(self, *a, **k):
        return self
    async def __aenter__(self):
        return self._conn
    async def __aexit__(self, et, ev, tb):
        return False


def _patch_charge():
    """把 service 依赖换成桩，直接驱动 process_recharge 的渠道失败路径。"""
    conn = _Conn()
    pool = _Pool(conn)
    order = {"order_no": "PA20260000000001", "status": "pending", "expire_at": None}

    svc._acquire_lock = _immediate("tok")
    svc._release_lock = _immediate(None)
    svc.get_pool = _immediate(pool)
    svc._load_order = _stub(order)
    svc._precheck_terminal_status = _immediate(None)
    svc._invoke_channel = _immediate({"success": False, "message": "模拟风控拦截"})
    return conn


def _immediate(v):
    """返回一个 async 调用：await 它即得 v（兼容 _acquire_lock/_invoke_channel/_release_lock 等）。"""
    async def _f(*a, **k):
        return v
    return _f


def _stub(order):
    async def _f(p, ono, uid):
        return order
    return _f


def test_failed_channel_returns_failed_no_crash():
    """渠道失败：返回 status=failed（干净失败响应），而非 UnboundLocalError。"""
    conn = _patch_charge()
    result = asyncio.run(svc.process_recharge("PA20260000000001", "u1"))
    assert result["status"] == "failed", f"期望 failed，got {result}"
    assert result["current_balance"] is None
    assert result["paid_at"] is None
    # 失败路径只标记订单失败，不产生余额/流水写入（事务提交仅持久化 failed 状态，属预期）


def test_settle_recharge_failure_returns_none_placeholders():
    """_settle_recharge 失败分支返回 (None, None)——修复 unbound 变量供外层安全组装。"""
    conn = _Conn()
    now, after = asyncio.run(svc._settle_recharge(
        conn, "PA20260000000001", "u1",
        {"success": False, "message": "渠道已停用"},
    ))
    assert (now, after) == (None, None)
