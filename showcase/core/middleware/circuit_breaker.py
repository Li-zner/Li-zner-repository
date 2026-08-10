import time

class SimpleBreaker:
    def __init__(self, fail_threshold=3, recover_timeout=30):
        self.fail_threshold = fail_threshold
        self.recover_timeout = recover_timeout
        self.fail_count = 0
        self.state = "CLOSED"                     # CLOSED / OPEN / HALF_OPEN
        self.last_fail_time = 0

    async def call(self, async_func, *args, **kwargs):
        if self.state == "OPEN":
            if time.time() - self.last_fail_time > self.recover_timeout:
                self.state = "HALF_OPEN"
            else:
                raise Exception("⛔ 熔断器已开启，服务暂时不可用")

        try:
            result = await async_func(*args, **kwargs)
            if self.state == "HALF_OPEN":
                self.state = "CLOSED"
                self.fail_count = 0
            return result
        except Exception as e:
            self.fail_count += 1
            self.last_fail_time = time.time()
            if self.fail_count >= self.fail_threshold:
                self.state = "OPEN"
                self.fail_count = 0
            raise e

# 熔断器注册表（按端点名隔离，避免单点故障影响全局）
_breakers: dict = {}

def get_breaker(name: str = "default") -> SimpleBreaker:
    """获取指定名称的熔断器实例（每个端点独立）"""
    if name not in _breakers:
        _breakers[name] = SimpleBreaker(fail_threshold=3, recover_timeout=30)
    return _breakers[name]

# 兼容旧代码：默认熔断器
deepseek_breaker = get_breaker("default")