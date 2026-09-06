"""通用有限状态机(迁移表驱动 + 守卫 + 钩子, 线程安全)—— 社区标准实现可复用版

适用场景: 电商订单流转(待支付 → 已支付 → 已发货 → 完成/关闭)、任务审批流、AI 任务生命周期——
一切「状态只能沿预定义路径变化」的领域对象。
设计要点(为什么这样做):
- 迁移表只允许声明过的路径: 非法跳转(已关闭的订单再支付)在入口就被拒绝,
  而不是散落在业务代码里靠 if 各自防守——路径即文档, 表即约束。
- 守卫(guard): 迁移前的业务校验(如「黑名单用户不允许支付」), 拒绝时状态不变。
- 钩子(on_transition): 状态变更的统一观测点(审计日志/发事件/清缓存), 不必在每个业务分支重复埋点;
  钩子在锁外调用——钩子里再触发本状态机的其他迁移不会死锁。
- 线程安全: 订单会被并发操作(用户取消 vs 系统发货), 「判定 + 迁移」必须串行。
迁移表格式: {事件: (允许的起始状态集合, 目标状态)}——一个事件一个目标, 多目标场景拆成多个事件。
来源(GitHub):
- python-statemachine: https://github.com/fgmacedo/python-statemachine
- XState 迁移表思想: https://github.com/statelyai/xstate
"""
import threading
from typing import Callable


class IllegalTransitionError(Exception):
    """非法迁移(路径不存在或守卫拒绝)时抛出; 上层通常映射 409/422"""


class StateMachine:
    """线程安全的表驱动状态机

    Args:
        transitions: 迁移表 {事件: (允许的起始状态集合, 目标状态)}。
        initial: 初始状态; 必须出现在至少一条迁移路径的起始集合中。
        guards: 可选 {事件: (当前状态) -> bool}, 返回 False 拒绝迁移。
        on_transition: 可选钩子 (事件, 旧状态, 新状态), 在状态变更并释放锁之后调用。
    """

    def __init__(self, transitions: dict, initial: str,
                 guards: dict | None = None,
                 on_transition: Callable[[str, str, str], None] | None = None) -> None:
        if not transitions:
            raise ValueError("迁移表不能为空")
        if not any(initial in from_states for from_states, _ in transitions.values()):
            raise ValueError(f"初始状态 {initial!r} 不在任何迁移路径的起始集合中")
        self._transitions = transitions
        self._guards = guards or {}
        self._on_transition = on_transition
        self._state = initial
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        """当前状态(读取走锁: 保证并发下读到的是完整迁移后的值)"""
        with self._lock:
            return self._state

    def can(self, event: str) -> bool:
        """查询当前状态是否允许该事件(只读, 不产生副作用)"""
        with self._lock:
            spec = self._transitions.get(event)
            return spec is not None and self._state in spec[0]

    def fire(self, event: str) -> str:
        """触发事件并迁移状态; 返回新状态; 路径不存在或守卫拒绝抛 IllegalTransitionError"""
        with self._lock:
            spec = self._transitions.get(event)
            if spec is None or self._state not in spec[0]:
                allowed = sorted(spec[0]) if spec else []
                raise IllegalTransitionError(
                    f"事件 {event!r} 不适用于当前状态 {self._state!r}(允许自: {allowed})")
            guard = self._guards.get(event)
            if guard is not None and not guard(self._state):
                raise IllegalTransitionError(
                    f"事件 {event!r} 被守卫拒绝(当前状态 {self._state!r})")
            old_state = self._state
            self._state = spec[1]
        # 钩子在锁外调用: 钩子内部若再触发本状态机, 不会因重入死锁
        if self._on_transition is not None:
            self._on_transition(event, old_state, self._state)
        return self._state


def _self_check() -> None:
    """最小自检: 订单正常流转 / 非法跳转被拒 / 守卫生效 / 钩子审计 / 并发只成功一次"""
    audit: list = []
    order = StateMachine(
        transitions={
            "pay": ({"created"}, "paid"),
            "ship": ({"paid"}, "shipped"),
            "deliver": ({"shipped"}, "done"),
            "cancel": ({"created", "paid"}, "cancelled"),
        },
        initial="created",
        on_transition=lambda ev, old, new: audit.append((ev, old, new)),
    )
    assert order.can("pay") and not order.can("ship"), "created 只能 pay, 不能直接 ship"
    assert order.fire("pay") == "paid"
    try:
        order.fire("deliver")  # paid 不允许直接发货完成
    except IllegalTransitionError:
        pass
    else:
        raise AssertionError("非法跳转必须被拒绝")
    assert order.fire("ship") == "shipped" and order.fire("deliver") == "done"
    assert audit == [("pay", "created", "paid"),
                     ("ship", "paid", "shipped"),
                     ("deliver", "shipped", "done")], "钩子应完整记录审计轨迹"

    guarded = StateMachine(
        transitions={"pay": ({"created"}, "paid")},
        initial="created",
        guards={"pay": lambda state: False},  # 示例: 黑名单用户拒绝支付
    )
    try:
        guarded.fire("pay")
    except IllegalTransitionError:
        pass
    else:
        raise AssertionError("守卫拒绝必须不迁移")
    assert guarded.state == "created", "守卫拒绝后状态不变"

    # 并发竞态: 用户取消 vs 系统发货, 都只允许成功一个
    racing = StateMachine(
        transitions={
            "pay": ({"created"}, "paid"),
            "cancel": ({"created"}, "cancelled"),
        },
        initial="created")
    outcomes: list = []

    def race(event: str) -> None:
        try:
            outcomes.append(racing.fire(event))
        except IllegalTransitionError:
            outcomes.append("rejected")

    threads = [threading.Thread(target=race, args=(ev,)) for ev in ("cancel", "pay")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count("rejected") == 1, "并发双事件必须恰好一个成功一个被拒"
    assert racing.state in ("paid", "cancelled"), "终态必须是两个合法目标之一"
    print("状态机 自检通过")


if __name__ == "__main__":
    _self_check()
