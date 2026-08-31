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


class LoginRequest(BaseModel):
    """登录请求（P2 #17：Pydantic 校验 + 自动生成 OpenAPI 文档）"""
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class ChangePasswordRequest(BaseModel):
    """修改密码请求"""
    old_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=6)


async def resolve_login_username(username: str) -> str:
    """把手机号解析为内部 username（phone_{phone}）；非手机号原样返回"""
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
async def login(payload: LoginRequest):
    """登录（Pydantic 校验自动文档，P2 #17；请求体限流见中间件层）"""
    username = payload.username
    password = payload.password
    # 支持手机号作为账号登录（账号即手机号）
    username = await resolve_login_username(username)
    user = await authenticate_user(username, password)
    if not user:
        raise HTTPException(401, "Invalid username or password")
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
    logger.info(f"🔑 密码已修改: username={username}")
    return {"message": "密码修改成功"}
