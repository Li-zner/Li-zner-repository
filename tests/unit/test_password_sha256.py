"""C7 密码 SHA-256 预处理全链路 — 离线单测

覆盖：新方案（$bcrypt-sha256$ 前缀）、超长密码 roundtrip、错误密码拒绝、
存量 $2b$ 哈希兼容、存量 72 字节截断路径、emoji 多字节、is_legacy_hash 判定。
纯逻辑测试，不依赖 DB / 网络。
"""
import os

# config.py 对 ADMIN_PASSWORD 缺失 fail-loudly，测试环境先注入（与 conftest 密钥策略一致）
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password")

from passlib.context import CryptContext

from app.core.config import PASSWORD_MAX_BYTES, BCRYPT_MAX_BYTES
from app.core.password import (
    SHA256_PREFIX, hash_password, is_legacy_hash, verify_password,
)

# 存量创建端模拟：改造前的纯 bcrypt（passlib 隐式 72 字节截断）
_legacy_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


class TestHashPassword:
    def test_new_hash_uses_sha256_prefix(self):
        h = hash_password("normal-password-123")
        assert h.startswith(SHA256_PREFIX)
        assert not h.startswith("$2b$")

    def test_over_72_bytes_roundtrip(self):
        # 100 字节 ASCII + 中文，超出 bcrypt 72 字节上限
        pw = "a" * 100 + "密码中文字符" * 10
        h = hash_password(pw)
        assert len(h) > 60  # bcrypt_sha256 哈希比纯 bcrypt 长
        assert verify_password(pw, h) is True

    def test_wrong_password_rejected(self):
        h = hash_password("correct-password")
        assert verify_password("wrong-password", h) is False

    def test_emoji_multibyte_roundtrip(self):
        # emoji 为 4 字节 UTF-8，60 个 emoji = 240 字节，远超 72
        pw = "安全密码" + chr(0x1F499) * 60
        h = hash_password(pw)
        assert verify_password(pw, h) is True

    def test_empty_or_none_hash_rejected(self):
        assert verify_password("anything", "") is False
        assert verify_password("anything", None) is False

    def test_short_password_roundtrip(self):
        pw = "abc123"
        assert verify_password(pw, hash_password(pw)) is True


class TestLegacyCompat:
    def test_legacy_b2b_hash_verifies(self):
        # 存量短密码：纯 bcrypt 哈希仍可验证
        h = _legacy_ctx.hash("old-password")
        assert h.startswith("$2b$")
        assert verify_password("old-password", h) is True

    def test_legacy_long_password_truncation_path(self):
        # 存量超长密码：创建端隐式截断 72 字节，验证端同样截断 → 一致
        pw = "a" * 100
        h = _legacy_ctx.hash(pw)  # 等价于 hash(bytes[:72])
        assert verify_password(pw, h) is True
        # 前 72 字节相同、后段不同的字符串应同样通过（截断语义）
        assert verify_password("a" * 72 + "zzzz", h) is True
        # 前 72 字节不同则拒绝
        assert verify_password("b" * 72 + "aaaa", h) is False

    def test_legacy_hash_upgradeable_detection(self):
        assert is_legacy_hash(_legacy_ctx.hash("x")) is True
        assert is_legacy_hash(hash_password("x")) is False
        assert is_legacy_hash("") is False


class TestConfig:
    def test_password_max_bytes_exported(self):
        # 新方案策略上限（防 DoS），应大于 bcrypt 技术上限 72
        assert PASSWORD_MAX_BYTES > BCRYPT_MAX_BYTES
        assert PASSWORD_MAX_BYTES >= 128
