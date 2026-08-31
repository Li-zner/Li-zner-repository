"""支付幂等集成测试（需运行中的实例，手动加 -m integration 运行）

验证「同一订单重复支付只成功一次」：
- 创建充值订单 → 支付 → 余额增加一次
- 再次支付同一订单 → 被拒绝（已支付）→ 余额不变、流水只有一条
"""
import httpx
import pytest

NGINX = "http://localhost:10090"
ADMIN = {"username": "admin", "password": "Admin@2026"}

pytestmark = pytest.mark.integration


def _login() -> str:
    r = httpx.post(f"{NGINX}/api/login", json=ADMIN, timeout=10)
    assert r.status_code == 200, f"登录失败: {r.text}"
    return r.json()["access_token"]


def _wallet(token: str) -> dict:
    r = httpx.get(f"{NGINX}/api/payment/wallet", headers={"Authorization": f"Bearer {token}"}, timeout=10)
    assert r.status_code == 200
    return r.json()


def test_duplicate_pay_only_deducts_once():
    token = _login()
    headers = {"Authorization": f"Bearer {token}"}

    # 1. 创建充值订单
    r = httpx.post(f"{NGINX}/api/payment/recharge", headers=headers,
                   json={"amount": 10.0, "channel": "balance"}, timeout=10)
    assert r.status_code == 200, f"创建订单失败: {r.text}"
    order_no = r.json()["order_no"]
    assert order_no

    # 2. 第一次支付
    before = float(_wallet(token)["balance"])
    r = httpx.post(f"{NGINX}/api/payment/{order_no}/pay", headers=headers, timeout=10)
    assert r.status_code == 200, f"首次支付失败: {r.text}"
    after_first = float(_wallet(token)["balance"])
    assert after_first > before

    # 3. 重复支付同一订单（幂等：余额与流水不得重复）
    #    实现语义 = 幂等返回：已支付订单重复支付返回 200 + status=success（不重复扣款），
    #    与部分实现直接拒绝（400/409）都算幂等正确，核心不变式是下面两条。
    r2 = httpx.post(f"{NGINX}/api/payment/{order_no}/pay", headers=headers, timeout=10)
    assert r2.status_code in (200, 400, 409), f"重复支付应幂等, got {r2.status_code}: {r2.text}"
    if r2.status_code == 200:
        assert r2.json().get("status") == "success", f"重复支付 200 但状态异常: {r2.text}"

    # 4. 余额不得再次变化
    after_second = float(_wallet(token)["balance"])
    assert after_second == after_first, "重复支付导致余额再次变化！幂等失败"

    # 5. 流水只有一条充值记录
    tx = httpx.get(f"{NGINX}/api/payment/transactions", headers=headers, timeout=10).json()
    pay_txs = [t for t in tx.get("items", tx if isinstance(tx, list) else []) if t.get("order_no") == order_no]
    assert len(pay_txs) == 1, f"该订单流水应为 1 条, got {len(pay_txs)}"
