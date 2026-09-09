# ============================================================
# 可复用资产：雪花ID.py
# 来源：agent_gateway 生产机制独立参考实现（认证/缓存/限流/支付等模块同款语义）
# 实战验证：机制在生产项目运行；本件为独立可 import 参考
# 依赖：redis-py（lua 两条）/ 标准库
# 提取：2026-09-06；二次复用后请在来源行补注项目名
# ============================================================
"""雪花算法全局唯一 ID(64 位: 1 符号 + 41 时间戳 + 10 机器 + 12 序列)—— 社区标准实现可复用版

适用场景: 电商订单号、网关 trace_id、消息/事件去重键、分库分表主键——需要「时间有序 + 全局唯一」的 ID。
设计要点(为什么这样做):
- 时间戳在高位: ID 按生成时间天然有序, 数据库索引友好, 且可直接从 ID 反解生成时间。
- 机器位 + 序列位: 单机每毫秒最多 4096 个; 集群按 machine_id(0~1023)分区, 互不冲突。
- 时钟回拨防护: 系统时间被回拨(如 NTP 校时)时, 小幅回拨自旋等待追平, 大幅回拨直接报错拒绝发号——
  宁可短暂不可用也不能发出重复 ID(重复 ID 会污染订单/去重键, 代价远大于暂不可用)。
来源(GitHub):
- 推特原始设计: https://github.com/twitter-archive/snowflake
- Go 事实标准实现: https://github.com/bwmarrin/snowflake
"""
import threading
import time

# 默认纪元: 2024-01-01 UTC(毫秒)。自定义纪元让 41 位时间戳可用约 69 年(至 2093 年)
DEFAULT_EPOCH_MS = 1704038400000
# 最大回拨容忍(毫秒): 超过直接报错, 避免调用方被长时间自旋阻塞
MAX_BACKWARD_MS = 5


class ClockWentBackwardsError(Exception):
    """系统时间回拨超过容忍窗口时抛出; 排查时钟前禁止继续发号"""


class Snowflake:
    """线程安全的雪花 ID 发号器

    Args:
        machine_id: 机器号 0 ~ 2**machine_bits - 1, 集群内必须唯一。
        machine_bits: 机器位宽度(默认 10 位 = 支持 1024 个实例)。
        epoch_ms: 自定义纪元毫秒; 全部实例必须一致, 否则跨实例 ID 无法保证全序。
    """

    def __init__(self, machine_id: int = 0, machine_bits: int = 10,
                 epoch_ms: int = DEFAULT_EPOCH_MS) -> None:
        sequence_bits = 12  # 每毫秒 4096 个: 2**12
        max_machine = (1 << machine_bits) - 1
        if not 0 <= machine_id <= max_machine:
            raise ValueError(f"machine_id 必须在 0~{max_machine}, 当前为 {machine_id}")
        self._ts_shift = sequence_bits + machine_bits  # 时间戳左移位数(默认 22)
        self._machine_part = machine_id << sequence_bits
        self._sequence_mask = (1 << sequence_bits) - 1
        self._epoch_ms = epoch_ms
        self._lock = threading.Lock()
        self._last_ts_ms = -1
        self._sequence = 0

    def _now_ms(self) -> int:
        # time_ns 取整到毫秒: 避免浮点乘除的精度误差
        return time.time_ns() // 1_000_000

    def next_id(self) -> int:
        """生成下一个全局唯一 ID(线程安全; 同机严格递增)"""
        with self._lock:
            now = self._now_ms()
            if now < self._last_ts_ms:
                backward = self._last_ts_ms - now
                if backward > MAX_BACKWARD_MS:
                    raise ClockWentBackwardsError(
                        f"时钟回拨 {backward}ms 超过容忍 {MAX_BACKWARD_MS}ms, 拒绝发号")
                # 小幅回拨: 自旋等待系统时间追上上次发号时刻(标准处理, 见 bwmarrin/snowflake)
                while now <= self._last_ts_ms:
                    now = self._now_ms()
            if now == self._last_ts_ms:
                self._sequence = (self._sequence + 1) & self._sequence_mask
                if self._sequence == 0:
                    # 当前毫秒 4096 个序号耗尽: 自旋进入下一毫秒再发, 保证不重不发
                    while now <= self._last_ts_ms:
                        now = self._now_ms()
            else:
                self._sequence = 0
            self._last_ts_ms = now
            ts_part = (now - self._epoch_ms) << self._ts_shift
            return ts_part | self._machine_part | self._sequence


def _self_check() -> None:
    """最小自检: 多线程唯一性 / 趋势递增 / 时间可反解 / 大幅回拨拒绝发号"""
    sf = Snowflake(machine_id=7)
    ids: list = []
    lock = threading.Lock()

    def produce(count: int) -> None:
        local = [sf.next_id() for _ in range(count)]
        with lock:
            ids.extend(local)

    threads = [threading.Thread(target=produce, args=(200,)) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ids) == 2000 and len(set(ids)) == 2000, "2000 个并发 ID 必须全部唯一"
    assert ids == sorted(ids), "同机发号必须严格递增"
    parsed_ms = (ids[-1] >> 22) + DEFAULT_EPOCH_MS  # machine_bits=10 + sequence_bits=12
    now_ms = time.time_ns() // 1_000_000
    assert abs(now_ms - parsed_ms) < 60_000, "从 ID 反解的生成时间应接近当前时间"

    broken_clock = Snowflake(machine_id=7)
    broken_clock._last_ts_ms = broken_clock._now_ms() + 10_000  # 人为制造「未来」模拟大幅回拨
    try:
        broken_clock.next_id()
    except ClockWentBackwardsError:
        pass
    else:
        raise AssertionError("大幅时钟回拨必须拒绝发号")
    print("雪花ID 自检通过")


if __name__ == "__main__":
    _self_check()
