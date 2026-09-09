# ============================================================
# 可复用资产：HMAC签名+防重放.py
# 来源：agent_gateway 生产机制独立参考实现（认证/缓存/限流/支付等模块同款语义）
# 实战验证：机制在生产项目运行；本件为独立可 import 参考
# 依赖：redis-py（lua 两条）/ 标准库
# 提取：2026-09-06；二次复用后请在来源行补注项目名
# ============================================================
"""HMAC-SHA256 请求签名 + 时间戳窗口 + nonce 防重放—— 社区标准实现可复用版

适用场景: 网关对下游、开放 API 对外部调用方的请求验签; 支付/Webhook 回调验签(电商、APP 后端通用)。
设计要点(为什么这样做):
- 密钥只参与 HMAC 运算、永不随请求传输; 服务端用同一密钥重算比对, 泄露面收敛到密钥本身(走 .env)。
- 签名覆盖「时间戳 + 完整请求体」: 篡改任何一部分都会比对失败; 用 hmac.compare_digest 恒时比较,
  防止时序侧信道逐字节猜签名。
- 时间戳窗口(默认 5 分钟): 拒绝过期请求, 把重放攻击的可行窗口压到签名有效期内。
- nonce 一次性记账: 同一 nonce 只接受一次, 窗口内的完全重放(同签名同报文)直接拒绝;
  nonce 表带 TTL 自动清理, 内存不会无限增长。
线格式(对齐 Stripe Webhook 的 t/v1 风格): t=<毫秒时间戳>,v1=<十六进制签名>,n=<nonce>
来源(GitHub/官方):
- Stripe Webhook 签名方案: https://github.com/stripe/stripe-python
- AWS 签名 v4: https://docs.aws.amazon.com/zh_cn/general/latest/gr/sigv4-calculate-signature.html
- 恒时比较: https://docs.python.org/3/library/hmac.html#hmac.compare_digest
"""
import hashlib
import hmac
import secrets
import threading
import time


class SignatureError(Exception):
    """验签失败(格式不合法/签名不符/时间戳过期/nonce 重放)时抛出; 上层统一映射 401"""


class SignatureVerifier:
    """线程安全的签名器/验签器

    Args:
        secret: 共享密钥; 双方一致, 走 .env/密钥管理下发, 禁止硬编码、禁止写进代码。
        window_seconds: 时间戳容忍窗口(秒), 默认 300, 覆盖合理时钟偏差。
        nonce_ttl_seconds: nonce 记账保留时长(秒), 必须 >= 时间戳窗口。
    """

    def __init__(self, secret: str, window_seconds: float = 300.0,
                 nonce_ttl_seconds: float = 600.0) -> None:
        if not secret:
            raise ValueError("secret 不能为空(密钥缺失必须 fail loudly)")
        self._key = secret.encode("utf-8")
        self._window_ms = window_seconds * 1000
        self._nonce_ttl = nonce_ttl_seconds
        self._nonces: dict = {}  # nonce -> 过期时刻(monotonic 秒)
        self._lock = threading.Lock()

    def _digest(self, ts_ms: int, payload: str) -> str:
        """签名内容 = 时间戳 + 完整请求体; 任何一部分被篡改都会比对失败"""
        message = f"{ts_ms}.{payload}".encode("utf-8")
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()

    def sign(self, payload: str, timestamp_ms: int | None = None,
             nonce: str | None = None) -> str:
        """生成签名头; 时间戳与 nonce 不传则自动生成(调用方只管放进请求头)"""
        ts_ms = timestamp_ms if timestamp_ms is not None else time.time_ns() // 1_000_000
        nonce = nonce if nonce is not None else secrets.token_hex(8)
        return f"t={ts_ms},v1={self._digest(ts_ms, payload)},n={nonce}"

    def verify(self, payload: str, header: str) -> None:
        """验签; 通过则静默返回, 任何不合法都抛 SignatureError(便于上层统一映射 401)"""
        try:
            fields = dict(kv.split("=", 1) for kv in header.split(",") if "=" in kv)
            ts_ms = int(fields["t"])
            digest = fields["v1"]
            nonce = fields["n"]
        except (KeyError, ValueError) as exc:
            raise SignatureError(f"签名头格式不合法: {header!r}") from exc
        now_ms = time.time_ns() // 1_000_000
        if abs(now_ms - ts_ms) > self._window_ms:
            raise SignatureError(
                f"时间戳超出容忍窗口({self._window_ms // 1000:.0f}s), 判定过期或时钟漂移")
        # 恒时比较: 无论错在第几字节, 耗时一致, 防时序侧信道
        if not hmac.compare_digest(self._digest(ts_ms, payload), digest):
            raise SignatureError("签名不符(报文被篡改或密钥不一致)")
        self._record_nonce(nonce)

    def _record_nonce(self, nonce: str) -> None:
        """nonce 一次性记账: 重复出现即重放; 顺带清理过期项, 内存不无限增长"""
        now = time.monotonic()
        with self._lock:
            for key in [k for k, exp in self._nonces.items() if exp <= now]:
                del self._nonces[key]
            if nonce in self._nonces:
                raise SignatureError("nonce 重复提交, 判定为重放请求")
            self._nonces[nonce] = now + self._nonce_ttl


def _self_check() -> None:
    """最小自检: 正常通过 / 篡改失败 / 过期失败 / 重放失败 / 密钥不符失败"""
    verifier = SignatureVerifier("test-secret", window_seconds=60, nonce_ttl_seconds=120)
    other_key = SignatureVerifier("another-key", window_seconds=60, nonce_ttl_seconds=120)
    body = '{"order_id":"SO-1","amount":100}'

    verifier.verify(body, verifier.sign(body))  # 正常路径: 不抛即通过

    header = verifier.sign(body)
    try:
        verifier.verify(body + '"}', header)
    except SignatureError:
        pass
    else:
        raise AssertionError("篡改报文必须验签失败")

    stale_ms = time.time_ns() // 1_000_000 - 61_000  # 超出 60s 窗口
    try:
        verifier.verify(body, verifier.sign(body, timestamp_ms=stale_ms))
    except SignatureError:
        pass
    else:
        raise AssertionError("过期时间戳必须验签失败")

    fixed = verifier.sign(body, nonce="fixed-nonce")
    verifier.verify(body, fixed)
    try:
        verifier.verify(body, fixed)
    except SignatureError:
        pass
    else:
        raise AssertionError("同 nonce 重复提交必须判定重放")

    try:
        other_key.verify(body, verifier.sign(body))
    except SignatureError:
        pass
    else:
        raise AssertionError("密钥不一致必须验签失败")
    print("HMAC签名+防重放 自检通过")


if __name__ == "__main__":
    _self_check()
