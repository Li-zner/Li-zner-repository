"""库存/余额原子扣减 + 防超卖(Redis Lua + DB 乐观锁两套方案)—— 社区标准实现可复用版

适用场景: 电商秒杀库存、APP 余额/配额扣减、AI 后端 token 配额——一切「绝不能扣成负数」的计数资源。
两套方案(生产按架构二选一, 也可组合: Redis 挡量 + DB 兜底):
- Redis Lua: 单命令原子「检查 + 扣减」, 高并发秒杀首选; 附带回补脚本(退款/超时回补)。
- DB 条件更新(乐观锁): UPDATE ... SET stock = stock - N WHERE id = ? AND stock >= ? AND version = ?,
  用影响行数判定成败——不依赖 Redis、天然持久; 高并发下热点行竞争大, 常作兜底层。
进程内 StockHolder 仅用于本文件自检(复现同一语义); 生产必须用 Redis/DB 版。
来源(GitHub/官方):
- Redis 原子 DECR 模式: https://redis.io/commands/decr/
- Lua 脚本(EVAL)语义: https://redis.io/docs/latest/commands/eval/
- MySQL 条件更新影响行数: https://dev.mysql.com/doc/refman/8.0/en/update.html
"""
import threading

# 原子扣减: 余量足够才扣并返回扣后余量; 不足返回 -1(不做部分扣减, 避免「扣一半」的账实不符)
STOCK_DEDUCT_LUA = """
local remain = tonumber(redis.call("GET", KEYS[1]) or "0")
local want = tonumber(ARGV[1])
if remain >= want then
    return redis.call("DECRBY", KEYS[1], want)
end
return -1
"""

# 回补(归还): 退款、支付超时未支付的库存释放; 用 INCRBY 与扣减同键位
STOCK_RESTORE_LUA = """
return redis.call("INCRBY", KEYS[1], tonumber(ARGV[1]))
"""

# DB 乐观锁扣减: 影响行数 = 1 即成功; = 0 表示余量不足或版本冲突(重读重试或直接拒绝)
# 配套约定: stock 加 CHECK 约束(stock >= 0)兜底; version 字段防并发覆盖更新
STOCK_DEDUCT_SQL = (
    "UPDATE inventory "
    "SET stock = stock - %s, version = version + 1 "
    "WHERE sku_id = %s AND stock >= %s AND version = %s"
)


class StockHolder:
    """进程内库存容器: 复现 Redis/DB 版「条件 + 原子」语义, 供自检与单测使用

    为什么单独抽出来: 防超卖的核心是「检查与扣减在同一临界区」,
    用进程内锁模拟生产里的 Redis 单线程/DB 行锁语义, 让语义可以被自动化验证。
    """

    def __init__(self, stock: int) -> None:
        if stock < 0:
            raise ValueError("初始库存不能为负")
        self._stock = stock
        self._version = 0
        self._lock = threading.Lock()

    @property
    def stock(self) -> int:
        """当前余量(线程安全读取)"""
        with self._lock:
            return self._stock

    def deduct(self, want: int) -> bool:
        """原子扣减: 余量不足整体失败(不做部分扣减); 返回是否成功"""
        if want <= 0:
            raise ValueError("扣减量必须为正")
        with self._lock:
            if self._stock >= want:
                self._stock -= want
                self._version += 1
                return True
            return False

    def restore(self, amount: int) -> None:
        """回补(退款/超时释放); 回补不校验上限, 因为归还量来自真实扣减记录"""
        if amount <= 0:
            raise ValueError("回补量必须为正")
        with self._lock:
            self._stock += amount
            self._version += 1


def _self_check() -> None:
    """最小自检: 并发扣减不超卖 / 不足整体失败 / 回补归还 / 非法入参拒绝"""
    holder = StockHolder(100)
    success: list = []
    lock = threading.Lock()

    def buy_one() -> None:
        ok = holder.deduct(1)
        with lock:
            success.append(ok)

    threads = [threading.Thread(target=buy_one) for _ in range(200)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert success.count(True) == 100, "100 库存对 200 人抢购, 恰好成交 100 单"
    assert success.count(False) == 100, "其余 100 单应整体失败(不部分扣减)"
    assert holder.stock == 0, "防超卖: 最终余量必须为 0, 不能为负"

    assert holder.deduct(1) is False, "空库存再扣必须失败"
    holder.restore(30)
    assert holder.stock == 30, "回补应足额归还"

    try:
        holder.deduct(0)
    except ValueError:
        pass
    else:
        raise AssertionError("扣减量必须为正, 0 与负数应拒绝")
    print("原子扣减-防超卖 自检通过")


if __name__ == "__main__":
    _self_check()
