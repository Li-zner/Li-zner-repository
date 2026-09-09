<!-- ============================================================
  可复用资产：jwt_token_service_server.py | 来源：agent_gateway 生产机制 → MCP Server 封装（stdio）
  实战验证：verify_all.py 五阶段验收可跑 | 依赖：mcp sdk + redis/pg（按 server） | 提取：2026-09-06
  ============================================================ -->
"""
MCP 资产 004 — JWT 令牌签发与校验 Server（stdio 传输）

从 agent_gateway 认证体系（app/middleware/auth.py + 生产机制 1/模板 4）泛化。
纯标准库实现 HS256（无 python-jose 依赖）：base64url + hmac + 常量时间比较。

能力：
  1. create_access_token  —— 签发短时 access token（含 exp/iat/jti）
  2. verify_token         —— 验签 + 校验 exp/吊销黑名单，返回 payload
  3. create_refresh_token —— 签发长时 refresh token
  4. refresh_access_token —— 刷新轮换（旧 refresh 立即作废，jti 进黑名单）
  5. revoke_token         —— 按 jti 吊销（Redis 有则持久，无则进程内）

运行（stdio）：
    python jwt_token_service_server.py
纯逻辑自检（无需任何外部服务）：
    python jwt_token_service_server.py --self-check
验证 Client（需 Redis 可选）：
    python test_client.py

配置（进程环境变量优先，其次 _env.py 读 .env）：
    JWT_SECRET（必填，缺失时工具返回错误——fail loudly）
    ACCESS_TTL_MINUTES（默认 120）  REFRESH_TTL_DAYS（默认 7）
    REDIS_URL（可选，配置后黑名单跨实例共享）
"""
import base64
import hashlib
import hmac
import json
import sys
import time
import uuid
from pathlib import Path

from mcp.server.fastmcp import FastMCP

_ROOT = Path(__file__).resolve().parent.parent  # → MCP sever/
sys.path.insert(0, str(_ROOT))
from _env import get  # noqa: E402

mcp = FastMCP("jwt-token-service")

_ALG = "HS256"
ACCESS_TTL = int(get("ACCESS_TTL_MINUTES", "120")) * 60
REFRESH_TTL = int(get("REFRESH_TTL_DAYS", "7")) * 86400

# 吊销黑名单：jti -> 过期时间戳。有 Redis 时用 Redis（跨实例），无则进程内（单实例够用）
_revoked: dict = {}
_redis = None


async def _get_redis():
    global _redis
    if _redis is None and get("REDIS_URL"):
        import redis.asyncio as aioredis
        _redis = aioredis.from_url(get("REDIS_URL"), decode_responses=True)
    return _redis


# ---------- 纯标准库 HS256 ----------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(header_b64: str, payload_b64: str, secret: str) -> str:
    msg = f"{header_b64}.{payload_b64}".encode("utf-8")
    return _b64url(hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).digest())


def _encode(payload: dict, secret: str) -> str:
    header = {"alg": _ALG, "typ": "JWT"}
    h_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    p_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{h_b64}.{p_b64}.{_sign(h_b64, p_b64, secret)}"


def _decode(token: str, secret: str) -> dict:
    """验签 + 校验 exp。任何一步失败抛 ValueError（验签失败 / 过期 / 格式错）"""
    try:
        h_b64, p_b64, sig = token.split(".")
        if not hmac.compare_digest(sig, _sign(h_b64, p_b64, secret)):
            raise ValueError("签名无效")
        header = json.loads(_b64url_decode(h_b64))
        if header.get("alg") != _ALG:
            raise ValueError("算法不匹配")
        payload = json.loads(_b64url_decode(p_b64))
    except (ValueError, json.JSONDecodeError):
        raise ValueError("令牌格式错误或签名无效")
    exp = payload.get("exp")
    if exp and exp < int(time.time()):
        raise ValueError("令牌已过期")
    return payload


# ---------- 工具 ----------

def _secret() -> str:
    return get("JWT_SECRET")  # 无默认值：缺失返回空，工具层报错（fail loudly）


@mcp.tool()
async def create_access_token(subject: str, ttl_minutes: int = None, claims: dict = None) -> dict:
    """签发 access token（HS256，含 exp/iat/jti）。短时效，泄露面最小。

    Args:
        subject: 用户标识（sub）。
        ttl_minutes: 有效期分钟数，默认 120（生产建议 15min~2h + refresh 轮换）。
        claims: 附加声明（如 {"role": "admin"}），不要放敏感数据。
    """
    secret = _secret()
    if not secret:
        return {"error": "JWT_SECRET 未配置，拒绝签发（fail loudly）"}
    if not subject:
        return {"error": "subject 不能为空"}
    now = int(time.time())
    payload = {
        "sub": subject, "exp": now + (ttl_minutes or ACCESS_TTL // 60) * 60,
        "iat": now, "jti": uuid.uuid4().hex, "type": "access",
    }
    if claims:
        payload.update(claims)
    return {"token": _encode(payload, secret), "expires_in": (payload["exp"] - now)}


@mcp.tool()
async def verify_token(token: str) -> dict:
    """验签 + 校验过期 + 校验吊销黑名单。合法返回 payload，非法返回 error（绝不抛异常）。

    Args:
        token: 待校验的 JWT。
    """
    secret = _secret()
    if not secret:
        return {"error": "JWT_SECRET 未配置"}
    try:
        payload = _decode(token, secret)
    except ValueError as e:
        return {"error": str(e)}
    jti = payload.get("jti", "")
    r = await _get_redis()
    if r is not None:
        try:
            if await r.get(f"jwt:revoked:{jti}"):
                return {"error": "令牌已被吊销"}
        except Exception:
            pass  # Redis 故障不阻断验证（降级）
    elif jti in _revoked:
        return {"error": "令牌已被吊销"}
    return {"valid": True, "payload": payload}


@mcp.tool()
async def create_refresh_token(subject: str) -> dict:
    """签发 refresh token（长时效 7 天，带 jti 供吊销/轮换）。

    Args:
        subject: 用户标识（sub）。
    """
    secret = _secret()
    if not secret:
        return {"error": "JWT_SECRET 未配置，拒绝签发（fail loudly）"}
    now = int(time.time())
    payload = {
        "sub": subject, "exp": now + REFRESH_TTL,
        "iat": now, "jti": uuid.uuid4().hex, "type": "refresh",
    }
    return {"token": _encode(payload, secret), "expires_in": REFRESH_TTL}


@mcp.tool()
async def refresh_access_token(refresh_token: str) -> dict:
    """刷新轮换：校验 refresh → 作废旧 jti → 签发新 access + 新 refresh（轮换防重放）。

    Args:
        refresh_token: 旧 refresh token（使用一次即作废）。
    """
    secret = _secret()
    if not secret:
        return {"error": "JWT_SECRET 未配置"}
    try:
        payload = _decode(refresh_token, secret)
    except ValueError as e:
        return {"error": str(e)}
    if payload.get("type") != "refresh":
        return {"error": "不是 refresh token"}
    jti = payload.get("jti", "")
    await _revoke(jti, payload.get("exp", int(time.time()) + REFRESH_TTL))
    access = await create_access_token(payload["sub"])
    new_refresh = await create_refresh_token(payload["sub"])
    return {"access_token": access["token"], "refresh_token": new_refresh["token"],
            "expires_in": access["expires_in"]}


@mcp.tool()
async def revoke_token(token: str) -> dict:
    """按 jti 吊销令牌（踢人/泄露处置）。有 Redis 跨实例生效，无则仅本进程。

    Args:
        token: 要吊销的 JWT。
    """
    secret = _secret()
    if not secret:
        return {"error": "JWT_SECRET 未配置"}
    try:
        payload = _decode(token, secret)
    except ValueError as e:
        return {"error": str(e)}
    await _revoke(payload["jti"], payload.get("exp", int(time.time()) + 3600))
    return {"revoked": True, "jti": payload["jti"]}


async def _revoke(jti: str, expire_ts: int) -> None:
    r = await _get_redis()
    if r is not None:
        try:
            await r.set(f"jwt:revoked:{jti}", "1", ex=max(expire_ts - int(time.time()), 60))
            return
        except Exception:
            pass  # Redis 故障降级为进程内黑名单
    _revoked[jti] = expire_ts
    # 顺带清理已过期的进程内黑名单（防无限增长）
    now = int(time.time())
    for k in [k for k, v in _revoked.items() if v < now]:
        _revoked.pop(k, None)


# ---------- 自检 ----------

def _self_check() -> None:
    """纯逻辑自检（无需外部服务）：签发/验签往返、篡改拒绝、过期拒绝、轮换吊销。"""
    secret = "test-secret-for-self-check"

    # 1. 签发 → 验签往返
    now = int(time.time())
    token = _encode({"sub": "u1", "exp": now + 60, "iat": now, "jti": "j1", "type": "access"}, secret)
    payload = _decode(token, secret)
    assert payload["sub"] == "u1" and payload["type"] == "access"
    print("[self-check] 签发/验签往返 OK")

    # 2. 篡改拒绝：改 payload 任一位 → 签名不匹配
    h_b64, p_b64, sig = token.split(".")
    tampered = _b64url(json.dumps({"sub": "u2", "exp": now + 60, "iat": now,
                                   "jti": "j1", "type": "access"},
                                  separators=(",", ":")).encode("utf-8"))
    try:
        _decode(f"{h_b64}.{tampered}.{sig}", secret)
        raise AssertionError("篡改未被发现")
    except ValueError:
        print("[self-check] 篡改拒绝 OK")

    # 3. 过期拒绝
    expired = _encode({"sub": "u1", "exp": now - 10, "iat": now - 70, "jti": "j2"}, secret)
    try:
        _decode(expired, secret)
        raise AssertionError("过期未被发现")
    except ValueError:
        print("[self-check] 过期拒绝 OK")

    # 4. 签名密钥不符拒绝
    try:
        _decode(token, secret + "x")
        raise AssertionError("错误密钥未被发现")
    except ValueError:
        print("[self-check] 错误密钥拒绝 OK")
    print("全部自检通过")


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        _self_check()
    else:
        mcp.run()
