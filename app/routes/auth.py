"""认证路由：登录 / 刷新 / 登出 / 修改密码

从 app/main.py 纯移动而来（2026-08-31 模块化，行为等价，零逻辑改动）。
支持手机号作为账号登录（账号即手机号）；JWT 认证主体在 middleware/auth。
"""
from pydantic import BaseModel, Field
from fastapi import APIRouter, Request, Depends, HTTPException

from ..core.logging import setup_logging
from ..core.db import get_pool
from ..core.config import PASSWORD_MAX_BYTES
from ..core.password import hash_password
from ..middleware.auth import (
    get_current_user, authenticate_user, create_token_pair,
    refresh_access_token, revoke_refresh_token,
)

logger = setup_logging()

router = APIRouter()

# ---------- 登录防暴力破解（P2 修复：密码登录原先可无限尝试）----------
_LOGIN_FAIL_LIMIT = 5
_LOGIN_LOCK_SECONDS = 900


def _client_ip(request: Request) -> str:
    """取可信客户端 IP（X-Forwarded-For 末跳 > 直连 IP），供失败计数键使用

    2026-09-09 审查 P1 修复：nginx 用 $proxy_add_x_forwarded_for 追加真实 IP，
    首跳是客户端可伪造的前缀——取首跳时防爆破锁既可被绕过（每次换伪造 IP）也可
    反向陷害（伪造受害者 IP 连错 5 次锁 15 分钟）；末跳才是我方 nginx 追加的可信值。
    CF-Connecting-IP 不在部署链路（nginx 直连），不采信。
    """
    ip = (request.headers.get("x-forwarded-for")
          or (request.client.host if request.client else ""))
    if ip and "," in ip:
        ip = ip.split(",")[-1].strip()
    return ip or "unknown"


async def _check_login_lock(ip: str, username: str):
    """登录前检查失败锁：达上限的 IP+用户名组合 15 分钟内拒绝"""
    from ..core.redis import get_redis
    r = await get_redis()
    if await r.get(f"login_lock:{ip}:{username}"):
        raise HTTPException(429, "尝试次数过多，请 15 分钟后再试")


async def _record_login_failure(ip: str, username: str):
    """登录失败累计：达上限写入锁定键（TTL 即锁时长，自愈）"""
    from ..core.redis import get_redis
    r = await get_redis()
    key = f"login_fail:{ip}:{username}"
    count = await r.incr(key)
    if count == 1:
        await r.expire(key, _LOGIN_LOCK_SECONDS)
    if count >= _LOGIN_FAIL_LIMIT:
        await r.setex(f"login_lock:{ip}:{username}", _LOGIN_LOCK_SECONDS, "1")
        await r.delete(key)


async def _clear_login_failures(ip: str, username: str):
    """登录成功清零失败计数"""
    from ..core.redis import get_redis
    r = await get_redis()
    await r.delete(f"login_fail:{ip}:{username}")


class LoginRequest(BaseModel):
    """登录请求（P2 #17：Pydantic 校验 + 自动生成 OpenAPI 文档）"""
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class ChangePasswordRequest(BaseModel):
    """修改密码请求"""
    old_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=6)


async def resolve_login_username(username: str) -> str:
    """登录名归一：裸手机号按 users.phone 查到 username（新版=手机号本身，旧版
    phone_ 前缀行兼容解析）；非手机号原样返回（GitHub 登录名直登）"""
    import re
    if username.startswith("phone_") or not re.match(r'^1\d{10}$', username):
        return username
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT username FROM users WHERE phone=$1", username)
        if row:
            return row["username"]
    return username


@router.post("/api/login")
async def login(payload: LoginRequest, request: Request):
    """登录（Pydantic 校验自动文档；失败计数防暴力破解，成功清零）"""
    username = payload.username
    password = payload.password
    client_ip = _client_ip(request)
    # 先解析再落锁（2026-09-07 审查 P1）：原实现检查用原始输入（手机号）、
    # 失败记账用解析后 username，两者键不一致 → 手机号登录可无限试密码
    username = await resolve_login_username(username)
    await _check_login_lock(client_ip, username)
    # 支持手机号作为账号登录（账号即手机号）
    user = await authenticate_user(username, password)
    if not user:
        await _record_login_failure(client_ip, username)
        raise HTTPException(401, "Invalid username or password")
    await _clear_login_failures(client_ip, username)
    pair = create_token_pair(username)
    # 审计：登录成功
    from ..core.audit import audit
    await audit(username, "login", {"method": "password"})
    return {"access_token": pair["access_token"], "refresh_token": pair["refresh_token"], "token_type": "bearer"}


@router.post("/api/refresh")
async def refresh_token(request: Request):
    """独立 refresh token 换新 token 对（轮换：旧 refresh 进黑名单）"""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Missing token")
    token = auth[len("Bearer "):]
    result = await refresh_access_token(token)
    return {
        "access_token": result["access_token"],
        "refresh_token": result["refresh_token"],
        "token_type": "bearer",
    }


@router.post("/api/logout")
async def logout(request: Request):
    """登出：撤销 refresh token（其 jti 进黑名单）
    显式校验格式（P0 #11：格式错误返回 401，杜绝"无效成功"）"""
    auth = request.headers.get("Authorization", "")
    if not auth or not auth.startswith("Bearer "):
        raise HTTPException(401, "Invalid token format")
    await revoke_refresh_token(auth[len("Bearer "):])
    return {"success": True}


@router.post("/api/user/change-password")
async def change_password(payload: ChangePasswordRequest, current_user: dict = Depends(get_current_user)):
    """修改当前账号密码：需校验旧密码，bcrypt 更新（A24）"""
    old_password = payload.old_password
    new_password = payload.new_password
    if len(new_password.encode("utf-8")) > PASSWORD_MAX_BYTES:
        raise HTTPException(400, f"密码长度不能超过 {PASSWORD_MAX_BYTES} 字节")  # C7：策略上限（SHA-256 预处理）
    if new_password == old_password:
        raise HTTPException(400, "新密码不能与旧密码相同")
    username = current_user["username"]
    user = await authenticate_user(username, old_password)
    if not user:
        raise HTTPException(400, "旧密码不正确")
    new_hashed = hash_password(new_password)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET hashed_password=$1 WHERE username=$2",
            new_hashed, username
        )
    # 改密吊销存量会话（2026-09-07 审查 P2）：记录改密时刻，refresh 流程发现
    # token 签发早于该时刻即拒绝——旧 refresh token 最长 30 天有效的窗口被关闭
    import time
    from ..core.redis import get_redis
    from ..core.config import REFRESH_TOKEN_EXPIRE_DAYS
    r = await get_redis()
    await r.set(
        f"auth:pwd_changed:{username}", str(int(time.time())),
        ex=REFRESH_TOKEN_EXPIRE_DAYS * 86400,
    )
    logger.info(f"密码已修改: username={username}")
    return {"message": "密码修改成功"}
