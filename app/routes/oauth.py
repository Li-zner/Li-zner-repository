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
from ..core.logging import setup_logging
from ..middleware.auth import oauth, get_user, create_access_token, create_refresh_token

logger = setup_logging()

router = APIRouter()


@router.get("/auth/github")
async def auth_github(request: Request):
    if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET:
        return JSONResponse(
            status_code=503,
            content={"detail": "GitHub 登录暂未配置，请联系管理员设置"}
        )
    return await oauth.github.authorize_redirect(request, GITHUB_REDIRECT_URI)


async def _resolve_github_user(pool, github_user: dict, gh_id: str, user_email: str, audit) -> tuple:
    """GitHub 身份解析（P1 账号接管修复）。

    旧实现按 username 查到人即登录：任何人与已有用户同名的 GitHub 账号都能接管该账号。
    新设计（手机号为主用户名）：
      1) github_id 命中 -> 老用户回访（username 可能已改绑为 phone_{手机号}）；
      2) 用户名被本地账号占用 -> 拒绝登录，要求绑定手机号后使用（返回 conflict 标记）；
      3) 均未命中 -> 以 login 为 username 新建（带 github_id），后续可改绑。
    返回 (user, conflict)：conflict=True 时调用方直接重定向，user 为 None。
    """
    username = github_user['login']
    if gh_id:
        # 短连接查询 github_id 身份（用户名已改绑的场景只能靠它找回）
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT username, role, email, phone FROM users WHERE github_id = $1",
                gh_id,
            )
        if row:
            return {"username": row["username"], "role": row["role"],
                    "email": row["email"], "phone": row["phone"]}, False
    async with pool.acquire() as conn:
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
    async with pool.acquire() as conn:
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
            "email": row["email"], "phone": row["phone"]}
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
        username = github_user['login']
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
        # 仅普通用户且未绑定手机号 → 标记试用受限；已绑手机用户再次登录不再受限（幂等）
        # 注意用 user["username"]：改绑过手机号的回访用户，username 已是 phone_{手机号}
        qp = ""
        if user and user.get("role") != "admin" and not user.get("phone"):
            pool = await get_pool()
            async with pool.acquire() as conn:
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
            redirect_url = f"{GATEWAY_PUBLIC_URL}/?token={access_token}&refresh_token={refresh_token}{qp}"
            return RedirectResponse(url=redirect_url)
        return RedirectResponse(url=f"{GATEWAY_PUBLIC_URL}/?error=github_user_creation_failed")
    except Exception as e:
        return RedirectResponse(url=f"{GATEWAY_PUBLIC_URL}/?error={str(e)}")
