"""GitHub OAuth 登录路由（授权跳转 + 回调）

从 app/main.py 纯移动而来（2026-08-31 模块化，行为等价，零逻辑改动）。
回调含自动注册、admin 提权守卫、试用受限标记；失败时重定向到网关错误页。
"""
import httpx
import secrets

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse

from ..core.db import get_pool
from ..core.config import (
    GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, GITHUB_REDIRECT_URI, GATEWAY_PUBLIC_URL,
    HTTP_TIMEOUT_MEDIUM,
)
from ..core.password import hash_password
from ..middleware.auth import oauth, get_user, create_access_token, create_refresh_token

router = APIRouter()


@router.get("/auth/github")
async def auth_github(request: Request):
    if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET:
        return JSONResponse(
            status_code=503,
            content={"detail": "GitHub 登录暂未配置，请联系管理员设置"}
        )
    return await oauth.github.authorize_redirect(request, GITHUB_REDIRECT_URI)


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
        username = github_user['login']
        from ..core.audit import audit
        user = await get_user(username)
        if not user:
            # 自动注册：随机密码（不可用密码登录），email 用 GitHub 主邮箱
            # UPSERT RETURNING 原子合并"创建或查询"，消灭并发竞态窗口（P0 #7）
            hashed = hash_password(secrets.token_urlsafe(24))
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "INSERT INTO users (username, hashed_password, role, display_name, email, avatar_url) "
                    "VALUES ($1,$2,'user',$3,$4,$5) "
                    "ON CONFLICT (username) DO UPDATE SET email = EXCLUDED.email "
                    "RETURNING username, role, email, phone",
                    username, hashed,
                    github_user.get('name') or github_user.get('login') or '',
                    user_email, github_user.get('avatar_url') or '',
                )
            user = {"username": row["username"], "role": row["role"],
                    "email": row["email"], "phone": row["phone"]}
            await audit(username, "register", {"method": "github"})
        # admin 提权守卫：GitHub 登录命中 admin 账号且 email 为空（admin 种子账号）→ 拒绝，防同名劫持
        if user and user.get("role") == "admin" and not user.get("email"):
            return RedirectResponse(url=f"{GATEWAY_PUBLIC_URL}/?error=admin_login_via_github_denied")
        # 仅普通用户且未绑定手机号 → 标记试用受限；已绑手机用户再次登录不再受限（幂等）
        qp = ""
        if user and user.get("role") != "admin" and not user.get("phone"):
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE users SET quota_limited = TRUE WHERE username = $1 AND phone IS NULL",
                    username,
                )
            qp = "&quota_limited=1"
        if user:
            await audit(username, "login", {"method": "github"})
            access_token = create_access_token({"sub": user["username"]})
            refresh_token = create_refresh_token({"sub": user["username"]})
            redirect_url = f"{GATEWAY_PUBLIC_URL}/?token={access_token}&refresh_token={refresh_token}{qp}"
            return RedirectResponse(url=redirect_url)
        return RedirectResponse(url=f"{GATEWAY_PUBLIC_URL}/?error=github_user_creation_failed")
    except Exception as e:
        return RedirectResponse(url=f"{GATEWAY_PUBLIC_URL}/?error={str(e)}")
