"""乐观锁余额更新单元测试（离线）

验证 _apply_balance_change 的重试语义：
- 版本冲突自动重读重试（上限 max_retries+1 次）
- 冲突持续则失败（返回 False）
- 成功返回 (True, before, after, version)
"""
import pytest
from decimal import Decimal

from app.payment import service as payment_service


class FakeConn:
    """模拟数据库连接：按脚本返回 fetchrow / execute 结果"""

    def __init__(self, rows, update_results):
        # rows: 每次 fetchrow 返回的余额/版本；update_results: 每次 execute 是否成功
        self._rows = list(rows)
        self._updates = list(update_results)

    async def fetchrow(self, sql, *args):
        if not self._rows:
            return None
        return self._rows.pop(0)

    async def execute(self, sql, *args):
        if not self._updates:
            return "UPDATE 0"
        return "UPDATE 1" if self._updates.pop(0) else "UPDATE 0"


@pytest.mark.asyncio
async def test_success_first_try():
    conn = FakeConn(
        rows=[{"balance": Decimal("100"), "version": 0}],
        update_results=[True],
    )
    ok, before, after, ver = await payment_service._apply_balance_change(
        conn, "u1", Decimal("-10")
    )
    assert ok is True
    assert before == Decimal("100")
    assert after == Decimal("90")
    assert ver == 0


@pytest.mark.asyncio
async def test_retry_after_conflict_then_success():
    """前两次版本冲突，第三次成功 → 应重试并最终成功"""
    conn = FakeConn(
        rows=[
            {"balance": Decimal("100"), "version": 0},
            {"balance": Decimal("100"), "version": 1},   # 重读（别人已改）
            {"balance": Decimal("90"), "version": 2},    # 再次重读
        ],
        update_results=[False, False, True],
    )
    ok, before, after, ver = await payment_service._apply_balance_change(
        conn, "u1", Decimal("-10")
    )
    assert ok is True
    assert before == Decimal("90")   # 以最后一次重读的余额为准
    assert after == Decimal("80")


@pytest.mark.asyncio
async def test_fail_after_all_conflicts():
    """持续冲突 → 达到重试上限后失败"""
    conn = FakeConn(
        rows=[
            {"balance": Decimal("100"), "version": 0},
            {"balance": Decimal("100"), "version": 1},
            {"balance": Decimal("100"), "version": 2},
            {"balance": Decimal("100"), "version": 3},
        ],
        update_results=[False, False, False, False],
    )
    ok, before, after, ver = await payment_service._apply_balance_change(
        conn, "u1", Decimal("-10"), max_retries=3
    )
    assert ok is False


@pytest.mark.asyncio
async def test_user_not_found():
    conn = FakeConn(rows=[], update_results=[])
    ok, before, after, ver = await payment_service._apply_balance_change(
        conn, "ghost", Decimal("-10")
    )
    assert ok is False
