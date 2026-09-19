"""GitHub OAuth 登录路由（授权跳转 + 回调 + 一次性授权码兑换）

从 app/main.py 纯移动而来（2026-08-31 模块化，行为等价，零逻辑改动）。
回调含自动注册、admin 提权守卫、试用受限标记；失败时重定向到网关错误页。
回调不再把长期令牌放入 URL，而是签发 60 秒有效的一次性授权码。
"""
import json
import time

import httpx
import secrets

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from ..core.db import get_pool
from ..core.config import (
    GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, GITHUB_REDIRECT_URI, GATEWAY_PUBLIC_URL,
    HTTP_TIMEOUT_MEDIUM,
)
from ..core.password import hash_password
from ..core.logging import setup_logging
from ..core.redis import get_redis
from ..middleware.auth import oauth, get_user, create_access_token, create_refresh_token
from .auth import _client_ip

logger = setup_logging()

router = APIRouter()

_OAUTH_CODE_TTL_SECONDS = 60
_OAUTH_CODE_PREFIX = "oauth_code:"

# 2026-09-12 清欠：exchange 无鉴权，加 IP 限频防滥用。
# 2026-09-12 二次修复（外部复核 P1）：改 Redis 原子限流（进程内字典在 Nginx 后
# 所有用户共享 request.client.host 且多实例不共享），IP 从可信代理头取
_EXCHANGE_WINDOW = 60
_EXCHANGE_MAX_PER_WINDOW = 10

_EXCHANGE_RATE_LUA = """
local t = redis.call('TIME')
local now_ms = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, now_ms - ARGV[1] * 1000)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then return 0 end
redis.call('ZADD', KEYS[1], now_ms, ARGV[3])
redis.call('EXPIRE', KEYS[1], ARGV[1])
return 1
"""


_CONSUME_CODE_LUA = """
local value = redis.call('GET', KEYS[1])
if value then redis.call('DEL', KEYS[1]) end
return value
"""


class OAuthCodeRequest(BaseModel):
    """OAuth 回调一次性授权码请求体。"""

    code: str = Field(min_length=32, max_length=128)


async def _store_oauth_tokens(access_token: str, refresh_token: str) -> str:
    """把令牌对写入 Redis 授权码，授权码只允许兑换一次。"""
    code = secrets.token_urlsafe(32)
    redis = await get_redis()
    await redis.set(
        f"{_OAUTH_CODE_PREFIX}{code}",
        json.dumps({"access_token": access_token, "refresh_token": refresh_token}),
        ex=_OAUTH_CODE_TTL_SECONDS,
    )
    return code


async def _consume_oauth_code(code: str) -> dict | None:
    """原子读取并删除授权码，避免并发重放。"""
    redis = await get_redis()
    raw = await redis.eval(_CONSUME_CODE_LUA, 1, f"{_OAUTH_CODE_PREFIX}{code}")
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        logger.warning("OAuth 授权码载荷损坏")
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _oauth_redirect_url(code: str, quota_query: str = "") -> str:
    """构造 fragment 回调地址，阻止授权码进入服务端访问日志。"""
    return f"{GATEWAY_PUBLIC_URL}/#/login?oauth_code={code}{quota_query}"


@router.get("/auth/github")
async def auth_github(request: Request):
    if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET:
        return JSONResponse(
            status_code=503,
            content={"detail": "GitHub 登录暂未配置，请联系管理员设置"}
        )
    return await oauth.github.authorize_redirect(request, GITHUB_REDIRECT_URI)


@router.post("/api/oauth/exchange")
async def exchange_oauth_code(request: Request, payload: OAuthCodeRequest):
    """原子兑换一次性 OAuth 授权码，返回短期访问令牌与刷新令牌。"""
    # IP 限频：10 次/分钟，Redis 原子滑动窗口（多实例共享）
    client_ip = _client_ip(request)
    redis = await get_redis()
    allowed = await redis.eval(
        _EXCHANGE_RATE_LUA, 1, f"oauth_exchange:{client_ip}",
        _EXCHANGE_WINDOW, _EXCHANGE_MAX_PER_WINDOW,
        str(time.time_ns()),
    )
    if not allowed:
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    tokens = await _consume_oauth_code(payload.code)
    if not tokens:
        raise HTTPException(status_code=400, detail="授权码无效或已过期")
    return {
        "access_token": tokens.get("access_token", ""),
        "refresh_token": tokens.get("refresh_token", ""),
        "token_type": "bearer",
    }


async def _resolve_github_user(pool, github_user: dict, gh_id: str, user_email: str, audit) -> tuple:
    """GitHub 身份解析（P1 账号接管修复）。

    旧实现按 username 查到人即登录：任何人与已有用户同名的 GitHub 账号都能接管该账号。
    新设计（手机号为主用户名）：
      1) github_id 命中 -> 老用户回访（username 可能已改绑为手机号）；
      2) 用户名被本地账号占用 -> 拒绝登录，要求绑定手机号后使用（返回 conflict 标记）；
      3) 均未命中 -> 以 login 为 username 新建（带 github_id），后续可改绑。
    返回 (user, conflict)：conflict=True 时调用方直接重定向，user 为 None。
    """
    # 用户名策略（2026-09-09 主人定稿）：用 GitHub 登录名做用户名（无 phone_ 前缀）；
    # 授权信息缺 login 时随机生成短 ID 兜底。GitHub 用户名不允许纯数字，
    # 与手机号用户名天然不冲突。
    username = github_user.get('login') or f"gh{secrets.token_hex(4)}"
    if gh_id:
        # 短连接查询 github_id 身份（用户名已改绑的场景只能靠它找回）
        async with pool.acquire(timeout=5) as conn:
            row = await conn.fetchrow(
                "SELECT username, role, email, phone, is_active FROM users WHERE github_id = $1",
                gh_id,
            )
        if row:
            return {"username": row["username"], "role": row["role"],
                    "email": row["email"], "phone": row["phone"],
                    "is_active": bool(row["is_active"])}, False
    async with pool.acquire(timeout=5) as conn:
        taken = await conn.fetchval(
            "SELECT 1 FROM users WHERE username = $1", username)
    if taken:
        # 同名冲突：绝不签发他人账号的 token；引导绑定手机号后以手机号为主用户名
        logger.warning(f"GitHub 登录用户名冲突，要求绑定手机号: login={username}")
        return None, True
    # 新建：随机密码（不可用密码登录），email 用 GitHub 主邮箱。
    # ON CONFLICT (username) DO NOTHING：并发竞态下没抢到即按占用处理，
    # 不再 DO UPDATE 合并（旧实现正是接管向量）
    hashed = hash_password(secrets.token_urlsafe(24))
    async with pool.acquire(timeout=5) as conn:
        row = await conn.fetchrow(
            "INSERT INTO users (username, hashed_password, role, display_name, email, avatar_url, github_id) "
            "VALUES ($1,$2,'user',$3,$4,$5,$6) "
            "ON CONFLICT (username) DO NOTHING "
            "RETURNING username, role, email, phone",
            username, hashed,
            github_user.get('name') or github_user.get('login') or '',
            user_email, github_user.get('avatar_url') or '', gh_id,
        )
    if row is None:
        logger.warning(f"GitHub 注册并发用户名冲突: login={username}")
        return None, True
    user = {"username": row["username"], "role": row["role"],
            "email": row["email"], "phone": row["phone"], "is_active": True}
    await audit(username, "register", {"method": "github", "github_id": gh_id})
    return user, False


@router.get("/auth/github/callback")
async def auth_github_callback(request: Request):
    try:
        token = await oauth.github.authorize_access_token(request)
        if not token:
            raise HTTPException(400, "获取 access_token 失败")
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_MEDIUM) as client:
            resp = await client.get(
                'https://api.github.com/user',
                headers={'Authorization': f'Bearer {token["access_token"]}'}
            )
            resp.raise_for_status()
            github_user = resp.json()
            user_email = github_user.get('email')
            if not user_email:
                resp = await client.get(
                    'https://api.github.com/user/emails',
                    headers={'Authorization': f'Bearer {token["access_token"]}'}
                )
                resp.raise_for_status()
                emails = resp.json()
                user_email = next((e['email'] for e in emails if e['primary']), None)
                if not user_email:
                    user_email = github_user['login'] + '@github.com'
        gh_id = str(github_user.get('id') or '')
        from ..core.audit import audit
        pool = await get_pool()
        # ---------- 身份解析（详见 _resolve_github_user：回访/冲突拒绝/新建+注册审计）----------
        user, conflict = await _resolve_github_user(pool, github_user, gh_id, user_email, audit)
        if conflict:
            return RedirectResponse(
                url=f"{GATEWAY_PUBLIC_URL}/?error=github_username_taken")
        # admin 提权守卫：GitHub 登录命中 admin 账号且 email 为空（admin 种子账号）→ 拒绝，防同名劫持
        if user and user.get("role") == "admin" and not user.get("email"):
            return RedirectResponse(url=f"{GATEWAY_PUBLIC_URL}/?error=admin_login_via_github_denied")
        if user and not user.get("is_active", True):
            return RedirectResponse(url=f"{GATEWAY_PUBLIC_URL}/?error=account_disabled")
        # 仅普通用户且未绑定手机号 → 标记试用受限；已绑手机用户再次登录不再受限（幂等）
        # 注意用 user["username"]：改绑过手机号的回访用户，username 已是手机号本身
        qp = ""
        if user and user.get("role") != "admin" and not user.get("phone"):
            pool = await get_pool()
            async with pool.acquire(timeout=5) as conn:
                await conn.execute(
                    "UPDATE users SET quota_limited = TRUE WHERE username = $1 AND phone IS NULL",
                    user["username"],
                )
                from ..core.quota import invalidate_user_cache
                await invalidate_user_cache(user["username"])
            qp = "&quota_limited=1"
        if user:
            await audit(user["username"], "login", {"method": "github"})
            access_token = create_access_token({"sub": user["username"]})
            refresh_token = create_refresh_token({"sub": user["username"]})
            code = await _store_oauth_tokens(access_token, refresh_token)
            redirect_url = _oauth_redirect_url(code, qp)
            return RedirectResponse(url=redirect_url)
        return RedirectResponse(url=f"{GATEWAY_PUBLIC_URL}/?error=github_user_creation_failed")
    except Exception:
        # 固定错误码（2026-09-07 审查 P2）：str(e) 可能含上游错误细节/URL，
        # 拼进重定向 URL 会反射给前端；细节只进日志
        logger.exception("GitHub OAuth 回调失败")
        return RedirectResponse(url=f"{GATEWAY_PUBLIC_URL}/?error=github_callback_failed")
