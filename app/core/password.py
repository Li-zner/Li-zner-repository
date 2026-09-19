"""密码哈希/校验统一模块（C7：超长密码 SHA-256 预处理全链路）

背景：bcrypt 只处理前 72 字节。改造前创建端拒绝 >72 字节密码、验证端按
72 字节截断，导致超长密码被截断降级（强度损失）。C7 改用 passlib
`bcrypt_sha256` 方案（SHA-256 预处理 → bcrypt），消除 72 字节硬上限。

策略（前缀标记内嵌哈希串，无需 DB 迁移）：
- 新哈希：前缀 `$bcrypt-sha256$`，完整密码参与 SHA-256 预处理，不截断。
- 存量哈希：前缀 `$2b$`（改造前创建），验证时按 72 字节截断兼容；
  成功登录后由 auth.authenticate_user 惰性升级为新方案。
"""
from passlib.context import CryptContext
from passlib.exc import UnknownHashError

from .config import BCRYPT_MAX_BYTES
from .logging import setup_logging

logger = setup_logging()

# 新/存量方案前缀标记（passlib 1.7.4 的 bcrypt_sha256 存储前缀）
SHA256_PREFIX = "$bcrypt-sha256$"

# 默认方案 bcrypt_sha256（新哈希走预处理）；存量 $2b$ 哈希自动按前缀识别
pwd_context = CryptContext(schemes=["bcrypt_sha256", "bcrypt"], deprecated="auto")


def hash_password(plain_password: str) -> str:
    """创建新哈希：SHA-256 预处理 + bcrypt（支持超长密码，无 72 字节上限）

    显式指定 scheme 而非依赖默认顺序，保证将来调整 schemes 顺序不改变语义。
    """
    return pwd_context.hash(plain_password, scheme="bcrypt_sha256")


def is_legacy_hash(hashed_password: str) -> bool:
    """是否为存量（改造前）哈希：登录成功后据此触发惰性升级"""
    if not hashed_password:
        return False
    try:
        return pwd_context.identify(hashed_password) == "bcrypt"
    except Exception:
        return False


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """校验密码：按前缀区分新/存量方案

    - 新方案（$bcrypt-sha256$）：完整字符串参与 SHA-256 预处理，不截断；
      只做前缀判断，sha256+bcrypt 由 passlib 内部完成。
    - 存量方案（$2b$）：与旧创建端一致的 72 字节截断，兼容历史密码。
    """
    if not hashed_password:
        return False
    try:
        if hashed_password.startswith(SHA256_PREFIX):
            # 新方案：不可截断（截断会改变 SHA-256 输入，导致验证失败）
            return pwd_context.verify(plain_password, hashed_password)
        # 存量方案：字节截断，与旧创建端（passlib 隐式截断）行为一致
        pw_bytes = plain_password.encode("utf-8")
        if len(pw_bytes) > BCRYPT_MAX_BYTES:
            logger.warning(
                "password_truncated_legacy",
                extra={"extra_fields": {"password_bytes": len(pw_bytes)}},
            )
            pw_bytes = pw_bytes[:BCRYPT_MAX_BYTES]
        return pwd_context.verify(pw_bytes, hashed_password)
    except (UnknownHashError, ValueError, TypeError):
        logger.warning("password_hash_invalid")
        return False
