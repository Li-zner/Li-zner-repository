"""
模拟支付渠道实现

提供三种模拟渠道：
1. balance — 余额支付（即时扣款）
2. simulated_alipay — 模拟支付宝（95%成功率，1-3秒延迟）
3. simulated_wxpay — 模拟微信支付（95%成功率，1-3秒延迟）

每个渠道实现 ChannelInterface，供 service.py 调用。
"""
import random
import asyncio
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from abc import ABC, abstractmethod
from ..core.config import (
    PAYMENT_SUCCESS_RATE,
    PAYMENT_SIMULATED_DELAY_MIN,
    PAYMENT_SIMULATED_DELAY_MAX,
    APP_ENV,
)
from ..core.logging import setup_logging

logger = setup_logging()


class ChannelInterface(ABC):
    """支付渠道接口"""

    @property
    @abstractmethod
    def channel_code(self) -> str:
        ...

    @abstractmethod
    async def pay(self, order: dict) -> dict:
        """
        执行支付
        返回: {
            "success": bool,
            "channel_order_no": str,
            "paid_at": str (ISO format),
            "message": str
        }
        """
        ...

    @abstractmethod
    async def refund(self, order: dict, amount: Decimal) -> dict:
        """
        执行退款
        返回: {
            "success": bool,
            "channel_order_no": str,
            "message": str
        }
        """
        ...


def _generate_channel_order_no(prefix: str) -> str:
    """生成模拟渠道流水号"""
    now = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{prefix}{now}{uuid.uuid4().hex[:12]}"


def _simulate_delay(min_s: float, max_s: float):
    """模拟支付处理延迟"""
    delay = random.uniform(min_s, max_s)
    return asyncio.sleep(delay)


def _simulate_success(success_rate: float) -> bool:
    """模拟支付成功率"""
    return random.random() < success_rate


# ============================================================
# 余额支付（即时扣款，100%成功）
# ============================================================
class BalanceChannel(ChannelInterface):
    @property
    def channel_code(self) -> str:
        return "balance"

    async def pay(self, order: dict) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        return {
            "success": True,
            "channel_order_no": _generate_channel_order_no("BAL"),
            "paid_at": now,
            "message": "余额支付成功",
        }

    async def refund(self, order: dict, amount: Decimal) -> dict:
        return {
            "success": True,
            "channel_order_no": _generate_channel_order_no("BAL"),
            "message": "余额退款成功",
        }


# ============================================================
# 模拟支付宝
# ============================================================
class SimulatedAlipayChannel(ChannelInterface):
    @property
    def channel_code(self) -> str:
        return "simulated_alipay"

    async def pay(self, order: dict) -> dict:
        await _simulate_delay(PAYMENT_SIMULATED_DELAY_MIN, PAYMENT_SIMULATED_DELAY_MAX)
        success = _simulate_success(PAYMENT_SUCCESS_RATE)
        now = datetime.now(timezone.utc).isoformat()
        if success:
            logger.info(f"模拟支付宝支付成功: order_no={order.get('order_no')}")
            return {
                "success": True,
                "channel_order_no": _generate_channel_order_no("SAP"),
                "paid_at": now,
                "message": "模拟支付宝支付成功",
            }
        else:
            logger.info(f"模拟支付宝支付失败: order_no={order.get('order_no')}")
            return {
                "success": False,
                "channel_order_no": "",
                "paid_at": now,
                "message": "模拟支付宝支付失败（模拟风控拦截）",
            }

    async def refund(self, order: dict, amount: Decimal) -> dict:
        await _simulate_delay(1.0, 3.0)
        return {
            "success": True,
            "channel_order_no": _generate_channel_order_no("SAP"),
            "message": "模拟支付宝退款成功",
        }


# ============================================================
# 模拟微信支付
# ============================================================
class SimulatedWechatChannel(ChannelInterface):
    @property
    def channel_code(self) -> str:
        return "simulated_wxpay"

    async def pay(self, order: dict) -> dict:
        await _simulate_delay(PAYMENT_SIMULATED_DELAY_MIN, PAYMENT_SIMULATED_DELAY_MAX)
        success = _simulate_success(PAYMENT_SUCCESS_RATE)
        now = datetime.now(timezone.utc).isoformat()
        if success:
            logger.info(f"模拟微信支付成功: order_no={order.get('order_no')}")
            return {
                "success": True,
                "channel_order_no": _generate_channel_order_no("SWX"),
                "paid_at": now,
                "message": "模拟微信支付成功",
            }
        else:
            logger.info(f"模拟微信支付失败: order_no={order.get('order_no')}")
            return {
                "success": False,
                "channel_order_no": "",
                "paid_at": now,
                "message": "模拟微信支付失败（模拟风控拦截）",
            }

    async def refund(self, order: dict, amount: Decimal) -> dict:
        await _simulate_delay(1.0, 3.0)
        return {
            "success": True,
            "channel_order_no": _generate_channel_order_no("SWX"),
            "message": "模拟微信退款成功",
        }


# ============================================================
# 渠道注册表
# ============================================================
_CHANNEL_REGISTRY = {
    "balance": BalanceChannel(),
    "simulated_alipay": SimulatedAlipayChannel(),
    "simulated_wxpay": SimulatedWechatChannel(),
}

# 充值允许的渠道白名单（P0 修复）：balance 渠道是"用余额支付"，若允许用于充值 =
# 无任何外部资金动作直接给钱包加钱（无限铸币），故充值仅开放两个模拟外部渠道
RECHARGE_ALLOWED_CHANNELS = ("simulated_alipay", "simulated_wxpay")

# 允许模拟充值的环境集（PAY-2，2026-09-19 审查）：与 sms.py 演示模式门控同一惯例。
# 模拟渠道随机 95% 成功即入账，生产配置下等于合法铸币；真渠道接入前，
# 只有开发/测试环境放行，其余（含 APP_ENV 未设置的默认态）一律 fail-closed。
_SIMULATION_ALLOWED_ENVS = ("test", "dev", "local", "development")


def recharge_allowed_channels() -> tuple:
    """当前环境允许充值的渠道列表（生产返回空 = 所有充值请求在白名单校验处被拒）。"""
    if APP_ENV in _SIMULATION_ALLOWED_ENVS:
        return RECHARGE_ALLOWED_CHANNELS
    return ()


def get_channel(channel_code: str) -> ChannelInterface:
    channel = _CHANNEL_REGISTRY.get(channel_code)
    if not channel:
        raise ValueError(f"不支持的支付渠道: {channel_code}")
    return channel
