# ============================================================
# 可复用资产：Token预算守卫.py
# 来源：agent_gateway 生产机制独立参考实现（认证/缓存/限流/支付等模块同款语义）
# 实战验证：机制在生产项目运行；本件为独立可 import 参考
# 依赖：redis-py（lua 两条）/ 标准库
# 提取：2026-09-06；二次复用后请在来源行补注项目名
# ============================================================
"""LLM Token 预算守卫(按日预算 + 单次上限, 原子判定扣减)—— 社区标准实现可复用版

适用场景: AI 后端为每个用户/租户控制 LLM 花费——日预算封顶、单次请求上限, 超限拒绝或降级到小模型。
设计要点(为什么这样做):
- 「判定 + 扣减」在同一临界区完成: 先查后扣的两步写法在并发下必然超预算(与幂等键竞态同理)。
- 按自然日窗口: 跨零点自动重置; 「每天 X 额度」的语义对用户可解释, 比滑动窗口好沟通。
- token 计数时机: 请求前用 max_tokens 预占或按历史均值估算, 响应后按实际用量校正差额。
- 单进程版适用单实例部署或作为二道防线; 多实例部署应把计数放 Redis 并用 Lua 原子化
  (可直接复用本目录「原子扣减-防超卖.py」的扣减 Lua, 语义同构: 余量足够才扣)。
来源(GitHub):
- litellm 预算管理(max_budget): https://github.com/BerriAI/litellm
- token 计数: https://github.com/openai/tiktoken
"""
import threading
from datetime import datetime


class BudgetExceededError(Exception):
    """预算耗尽或单次超限时抛出; 上层通常映射 HTTP 429 并提示用户明日恢复"""


class TokenBudget:
    """线程安全的进程内 Token 预算守卫

    Args:
        daily_limit: 每日 token 总预算(必须为正)。
        per_call_limit: 单次调用 token 上限; 0 表示不设单次上限。
    """

    def __init__(self, daily_limit: int, per_call_limit: int = 0) -> None:
        if daily_limit <= 0:
            raise ValueError("daily_limit 必须为正")
        if per_call_limit < 0:
            raise ValueError("per_call_limit 不能为负")
        self._daily_limit = daily_limit
        self._per_call_limit = per_call_limit
        self._lock = threading.Lock()
        self._day = self._today()
        self._used = 0

    @staticmethod
    def _today() -> str:
        """自然日标识(本地时区); 抽成静态方法便于测试时替身"""
        return datetime.now().strftime("%Y-%m-%d")

    def try_consume(self, tokens: int) -> bool:
        """原子「判定 + 扣减」: 预算内返回 True 并记账; 超限返回 False(不做部分扣减)"""
        if tokens <= 0:
            raise ValueError("tokens 必须为正")
        with self._lock:
            self._reset_if_new_day()
            if self._per_call_limit and tokens > self._per_call_limit:
                return False  # 单次超限: 直接拒绝, 不消耗任何预算
            if self._used + tokens > self._daily_limit:
                return False  # 日预算不足: 整体拒绝, 防止「扣一半」的账实不符
            self._used += tokens
            return True

    def consume_or_raise(self, tokens: int) -> None:
        """同 try_consume; 超限抛 BudgetExceededError(便于映射 HTTP 429)"""
        if not self.try_consume(tokens):
            raise BudgetExceededError("token 预算已耗尽或单次超限")

    def used_today(self) -> int:
        """当日已用量(线程安全; 跨日后再读会先重置)"""
        with self._lock:
            self._reset_if_new_day()
            return self._used

    def _reset_if_new_day(self) -> None:
        """跨日重置计数(调用方必须已持锁)"""
        today = self._today()
        if today != self._day:
            self._day = today
            self._used = 0


def _self_check() -> None:
    """最小自检: 单次上限拒绝 / 日预算恰好耗尽 / 超限拒绝 / 跨日重置"""
    budget = TokenBudget(daily_limit=1000, per_call_limit=300)

    assert budget.try_consume(300) is True, "预算内的单次上限值应成功"
    assert budget.try_consume(400) is False, "超过单次上限应整体拒绝"
    assert budget.used_today() == 300, "被拒绝的请求不应记账"

    assert budget.try_consume(300) is True, "预算内的调用应成功"
    assert budget.try_consume(100) is True, "日预算应允许恰好扣满"
    assert budget.used_today() == 700, "300+300+100 应记账 700"
    assert budget.try_consume(301) is False, "超过单次上限应拒绝"
    assert budget.try_consume(300) is True, "剩余 300 应允许恰好扣满"
    assert budget.used_today() == 1000
    assert budget.try_consume(1) is False, "预算耗尽后应拒绝"
    try:
        budget.try_consume(-5)
    except ValueError:
        pass
    else:
        raise AssertionError("负数扣减必须拒绝")

    budget._day = "2000-01-01"  # 注入旧日期, 模拟跨日
    assert budget.used_today() == 0, "跨日应自动重置"
    for _ in range(3):
        assert budget.try_consume(300) is True, "新一天预算应恢复可用"
    assert budget.try_consume(100) is True, "新一天应允许恰好扣满 1000"
    assert budget.used_today() == 1000
    assert budget.try_consume(1) is False, "新一天预算耗尽后同样拒绝"
    print("Token预算守卫 自检通过")


if __name__ == "__main__":
    _self_check()
