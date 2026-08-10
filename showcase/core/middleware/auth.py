import httpx
import uuid
import secrets
from datetime import datetime, timedelta
from fastapi import Request, HTTPException, Depends, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from passlib.context import CryptContext
from authlib.integrations.starlette_client import OAuth
from starlette.middleware.sessions import SessionMiddleware
from authlib.integrations.starlette_client import OAuth

from ..core.config import (
    SECRET_KEY, SECRET_KEY_OLD, ALGORITHM, ACCESS_TOKEN_EXPIRE_MINUTES,
    REFRESH_TOKEN_EXPIRE_DAYS,
    GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, SESSION_SECRET_KEY
)
from ..core.logging import setup_logging
from ..core.redis import get_redis

logger = setup_logging()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer()

# ---------- 数据库用户操作 ----------
import asyncpg
from ..core.config import POSTGRES_DSN

async def get_user(username: str):
    from ..core.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT username, hashed_password, role FROM users WHERE username=$1",
            username
        )
        if row:
            return {
                "username": row["username"],
                "hashed_password": row["hashed_password"],
                "role": row["role"] or "user"
            }
        return None

def verify_password(plain_password, hashed_password):
    if not hashed_password:
        return False
    if len(plain_password) > 72:
        logger.warning(
            "password_truncated",
            extra={"extra_fields": {"password_length": len(plain_password)}}
        )
        plain_password = plain_password[:72]
    return pwd_context.verify(plain_password, hashed_password)

async def authenticate_user(username: str, password: str):
    user = await get_user(username)
    if not user or not verify_password(password, user["hashed_password"]):
        return None
    return user

def create_access_token(data: dict):
    to_encode = data.copy()
    now = datetime.utcnow()
    expire = now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire, "iat": now, "type": "access", "jti": uuid.uuid4().hex})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token(data: dict):
    """签发独立 refresh token（长时效；type=refresh 区分，jti 用于黑名单）"""
    to_encode = data.copy()
    now = datetime.utcnow()
    expire = now + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode.update({"exp": expire, "iat": now, "type": "refresh", "jti": uuid.uuid4().hex})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_token_pair(username: str) -> dict:
    """签发 access + refresh 一对 token（登录/注册/第三方回调统一入口）"""
    return {
        "access_token": create_access_token({"sub": username}),
        "refresh_token": create_refresh_token({"sub": username}),
    }


def _decode_token(token: str, verify_exp: bool = True) -> dict:
    """用当前密钥 + 旧密钥依次尝试解码（支持轮换并行期）

    轮换后新 token 用新密钥验，旧 token 用 JWT_SECRET_OLD 仍可验，
    保证并行期内会话不断。全部失败才抛错。
    """
    keys = [SECRET_KEY]
    if SECRET_KEY_OLD:
        keys.append(SECRET_KEY_OLD)
    last_err = None
    for key in keys:
        try:
            return jwt.decode(
                token, key, algorithms=[ALGORITHM],
                options={"verify_exp": verify_exp},
            )
        except JWTError as e:
            last_err = e
    raise last_err or JWTError("token decode failed")

async def refresh_access_token(token: str) -> dict:
    """用独立 refresh token 换新 token 对（轮换：旧 refresh 进黑名单）

    返回 {"username", "access_token", "refresh_token"}；
    超过 REFRESH_MAX_DAYS 天未活跃或已撤销则拒绝。
    """
    from ..core.config import REFRESH_MAX_DAYS
    try:
        # verify_exp=True：强制校验 refresh token 自身有效期（30 天硬边界）
        payload = _decode_token(token, verify_exp=True)
        if payload.get("type") != "refresh":
            raise HTTPException(401, "Invalid refresh token")
        jti = payload.get("jti")
        username = payload.get("sub")
        if not username:
            raise HTTPException(401, "Invalid token")
        # 黑名单检查（登出/轮换后的旧 refresh）
        if jti:
            r = await get_redis()
            if await r.get(f"auth:refresh_blacklist:{jti}"):
                raise HTTPException(401, "Token revoked")
        iat = payload.get("iat")
        if iat:
            age_days = (datetime.utcnow() - datetime.utcfromtimestamp(iat)).days
            if age_days > REFRESH_MAX_DAYS:
                raise HTTPException(401, "会话已过期，请重新登录")
        user = await get_user(username)
        if user is None:
            raise HTTPException(401, "User not found")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(401, "Invalid token")
    # 轮换：旧 refresh 进黑名单，签发新对
    if jti:
        r = await get_redis()
        await r.set(
            f"auth:refresh_blacklist:{jti}", "1",
            ex=REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        )
    return {
        "username": username,
        "access_token": create_access_token({"sub": username}),
        "refresh_token": create_refresh_token({"sub": username}),
    }


async def revoke_refresh_token(token: str):
    """登出：把 refresh token 的 jti 加入黑名单（幂等，无效 token 静默忽略）"""
    try:
        payload = _decode_token(token, verify_exp=False)
        jti = payload.get("jti")
        if jti:
            r = await get_redis()
            await r.set(
                f"auth:refresh_blacklist:{jti}", "1",
                ex=REFRESH_TOKEN_EXPIRE_DAYS * 86400,
            )
    except Exception:
        pass

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    try:
        payload = _decode_token(token)
        # 独立 refresh token 不能当 access 用（缺 type 视为 access，兼容旧 token）
        if payload.get("type") not in (None, "access"):
            raise JWTError("not an access token")
        username: str = payload.get("sub")
        if username is None:
            raise HTTPException(401, "Invalid token")
    except JWTError:
        raise HTTPException(401, "Invalid token")
    user = await get_user(username)
    if user is None:
        raise HTTPException(401, "User not found")
    return user


oauth = OAuth()
oauth.register(
    name="github",
    client_id=GITHUB_CLIENT_ID,
    client_secret=GITHUB_CLIENT_SECRET,
    access_token_url="https://github.com/login/oauth/access_token",
    authorize_url="https://github.com/login/oauth/authorize",
    api_base_url="https://api.github.com/",
    client_kwargs={"scope": "user:email"},
    client_auth_method="client_secret_basic",
)