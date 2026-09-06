"""支付 P0/P1 修复回归测试（2026-09-05，见 app/payment/修复日志.md）：

1. 充值渠道白名单：balance 渠道用于充值 = 无资金动作直接加余额（无限铸币），必须拒绝
2. 扣费余额快照挪进事务 + FOR UPDATE 锁行：流水 before 使用事务内快照，账实衔接
"""
import asyncio
from decimal import Decimal

import pytest

import app.payment.service as svc
from app.payment.channels import RECHARGE_ALLOWED_CHANNELS


# ============================================================
# 1. 充值渠道白名单（P0）
# ============================================================
def test_recharge_whitelist_excludes_balance():
    assert "balance" not in RECHARGE_ALLOWED_CHANNELS
    assert "simulated_alipay" in RECHARGE_ALLOWED_CHANNELS
    assert "simulated_wxpay" in RECHARGE_ALLOWED_CHANNELS


def test_recharge_rejects_balance_channel():
    """无幂等键时探测短路（不触 Redis）；白名单校验在建单/取连接之前抛出，
    因此本用例不需要任何 DB/Redis 桩即可验证拒绝路径"""
    async def _run():
        await svc.create_recharge_order(
            user_id="u1", amount=Decimal("100"), payment_method="balance")

    with pytest.raises(ValueError, match="充值不支持该支付渠道"):
        asyncio.run(_run())


def test_recharge_accepts_whitelisted_channel_validation_only():
    """白名单内的渠道通过渠道校验（后续金额越界同样在触库前抛出，验证顺序无回归）"""
    async def _run():
        # 金额越界：说明已通过渠道白名单，走到金额校验（两道校验均在触库前）
        await svc.create_recharge_order(
            user_id="u1", amount=Decimal("99999999"), payment_method="simulated_alipay")

    with pytest.raises(ValueError, match="充值金额不能超过"):
        asyncio.run(_run())


# ============================================================
# 2. 扣费：事务内快照 + FOR UPDATE + 流水用事务内 before（P1）
# ============================================================
class _Tx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, et, ev, tb):
        return False


class _DeductConn:
    """最小桩：钱包行余额 3.0；记录收到的 SQL 以断言 FOR UPDATE 存在"""
    balance = Decimal("3.0")

    def __init__(self):
        self.sqls = []

    def transaction(self):
        return _Tx(self)

    async def fetchrow(self, sql, *a):
        self.sqls.append(sql)
        if "user_wallets" in sql:
            return {"balance": _DeductConn.balance}
        return None

    async def execute(self, sql, *a):
        self.sqls.append(sql)
        return "INSERT 0 1" if "INSERT" in sql else "UPDATE 1"


def test_deduct_uses_for_update_and_tx_snapshot(monkeypatch):
    """余额不足(3.0 < 应扣10)时：实扣=3.0；流水 before 必须用事务内快照（FOR UPDATE 读值）"""
    captured = {}

    async def _fake_apply(conn, user_id, delta, max_retries=3, tx_type="recharge"):
        captured["delta"] = delta
        captured["tx_type"] = tx_type
        # 真实实现会在事务内重读锁定行，此处模拟其返回的事务内 before
        return True, _DeductConn.balance, _DeductConn.balance + delta, 1

    async def _fake_tx(conn, order_no, user_id, tx_type, amount, before, after, remark, operator):
        captured["ledger_before"] = before
        captured["ledger_after"] = after
        return None

    monkeypatch.setattr(svc, "_apply_balance_change", _fake_apply)
    monkeypatch.setattr(svc, "_do_record_tx", _fake_tx)

    conn = _DeductConn()
    before, actual, after = asyncio.run(
        svc._apply_token_deduction(conn, "u1", Decimal("10"), 10000, "PA1", "s1", ""))

    # 余额读取必须走 FOR UPDATE（行锁使充值/扣费在本行串行）
    assert any("FOR UPDATE" in s for s in conn.sqls if "user_wallets" in s), \
        "钱包读取必须带 FOR UPDATE 行锁"
    # 不足按余额扣：快照 3.0
    assert actual == Decimal("3.0")
    assert captured["delta"] == Decimal("-3.0") and captured["tx_type"] == "consume"
    # 流水 before/after 用事务内快照，账实衔接（3.0 - 3.0 = 0）
    assert captured["ledger_before"] == Decimal("3.0")
    assert captured["ledger_after"] == Decimal("0.0")
    assert (before, after) == (Decimal("3.0"), Decimal("0.0"))


def test_deduct_full_when_balance_sufficient(monkeypatch):
    """余额充足：全额扣，流水 before 即锁定行余额"""
    _DeductConn.balance = Decimal("50.0")
    captured = {}

    async def _fake_apply(conn, user_id, delta, max_retries=3, tx_type="recharge"):
        return True, _DeductConn.balance, _DeductConn.balance + delta, 1

    async def _fake_tx(conn, order_no, user_id, tx_type, amount, before, after, remark, operator):
        captured["before"], captured["amount"] = before, amount

    monkeypatch.setattr(svc, "_apply_balance_change", _fake_apply)
    monkeypatch.setattr(svc, "_do_record_tx", _fake_tx)

    before, actual, after = asyncio.run(
        svc._apply_token_deduction(_DeductConn(), "u1", Decimal("10"), 10000, "PA2", "s1", ""))
    assert actual == Decimal("10.0")
    assert captured["before"] == Decimal("50.0") and captured["amount"] == Decimal("10.0")
    # 50 - 10 = 40
    assert (before, after) == (Decimal("50.0"), Decimal("40.0"))
    _DeductConn.balance = Decimal("3.0")  # 还原类属性，避免影响其他用例


# ============================================================
# 3. P2：部分退款判定（纯函数）
# ============================================================
from app.payment.refund import _decide_refund, _invoke_channel_refund


def _order(status="success", amount="100"):
    return {"status": status, "amount": Decimal(amount), "payment_method": "simulated_alipay"}


def test_decide_refund_rejects_non_success():
    with pytest.raises(ValueError, match="仅已成功"):
        _decide_refund(_order(status="pending"), Decimal("0"), None)
    with pytest.raises(ValueError, match="仅已成功"):
        _decide_refund(_order(status="failed"), Decimal("0"), None)


def test_decide_refund_rejects_over_and_fully_refund():
    # 已退 30，再退 80 超过剩余 70 → 拒绝
    with pytest.raises(ValueError, match="剩余可退"):
        _decide_refund(_order(), Decimal("30"), Decimal("80"))
    # 已退满 → 拒绝
    with pytest.raises(ValueError, match="已全额退款"):
        _decide_refund(_order(), Decimal("100"), None)
    # 0 元 → 拒绝
    with pytest.raises(ValueError, match="大于 0"):
        _decide_refund(_order(), Decimal("0"), Decimal("0"))


def test_decide_refund_partial_and_full_paths():
    # 部分退款后仍可继续退：已退 30，指定退 20
    assert _decide_refund(_order(), Decimal("30"), Decimal("20")) == Decimal("20")
    # 不传金额 → 默认退剩余全部 70
    assert _decide_refund(_order(), Decimal("30"), None) == Decimal("70")
    # partial_refunded 状态也可继续退
    assert _decide_refund(_order(status="partial_refunded"), Decimal("30"), None) == Decimal("70")


# ============================================================
# 4. P2：退款接渠道（渠道失败必须拦截，不得入账）
# ============================================================
def test_channel_refund_failure_blocks(monkeypatch):
    from app.payment import channels as ch_mod

    class _BadChannel:
        async def refund(self, order, amount):
            return {"success": False, "message": "模拟渠道拒绝"}

    monkeypatch.setattr(ch_mod, "get_channel", lambda code: _BadChannel())
    with pytest.raises(ValueError, match="渠道退款失败"):
        asyncio.run(_invoke_channel_refund(_order(), Decimal("30")))


def test_channel_refund_success_passes(monkeypatch):
    from app.payment import channels as ch_mod

    class _OkChannel:
        async def refund(self, order, amount):
            return {"success": True, "channel_order_no": "R1", "message": "ok"}

    monkeypatch.setattr(ch_mod, "get_channel", lambda code: _OkChannel())
    # 成功不抛异常即通过
    asyncio.run(_invoke_channel_refund(_order(), Decimal("30")))


# ============================================================
# 5. P2：用户侧渠道列表隐藏 balance
# ============================================================
def test_user_channels_hide_balance(monkeypatch):
    from app.payment import query as q

    class _Conn:
        async def fetch(self, sql, *a):
            return []  # 触发回退分支

    class _Pool:
        def acquire(self, *a, **k):
            return self

        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, et, ev, tb):
            return False

    async def _immediate_pool():
        return _Pool()

    monkeypatch.setattr(q, "get_pool", _immediate_pool)
    items = asyncio.run(q.get_channels())
    codes = [c["channel_code"] for c in items]
    assert "balance" not in codes, "用户侧渠道列表不得包含 balance 渠道"
    assert set(codes) == {"simulated_alipay", "simulated_wxpay"}
