"""控制台安全 Cookie：HttpOnly 保存令牌，降低 XSS 窃取风险。"""

from __future__ import annotations

import os

from fastapi import Request
from starlette.responses import Response

from .config import ACCESS_TOKEN_EXPIRE_MINUTES, REFRESH_TOKEN_EXPIRE_DAYS

ACCESS_COOKIE = "rag_console_access"
REFRESH_COOKIE = "rag_console_refresh"
CONSOLE_AUTH_HEADER = "X-Console-Auth"
_COOKIE_PATH = os.getenv("RAG_CONSOLE_COOKIE_PATH", "/")
if not _COOKIE_PATH.startswith("/"):
    _COOKIE_PATH = "/"
_FORCE_SECURE = os.getenv("RAG_CONSOLE_COOKIE_SECURE", "0") == "1"


def _secure_request(request: Request) -> bool:
    """本机 HTTP 允许非 Secure；生产 HTTPS 强制 Secure。"""
    forwarded = request.headers.get("x-forwarded-proto", "")
    forwarded_https = any(
        item.strip().lower() == "https"
        for item in forwarded.split(",")
    )
    return request.url.scheme == "https" or forwarded_https


def is_console_cookie_request(request: Request) -> bool:
    """控制台显式声明 Cookie 模式，避免普通站点继续接收 body 令牌。"""
    return request.headers.get(CONSOLE_AUTH_HEADER, "").lower() == "cookie"


def set_console_cookies(response: Response, pair: dict,
                        request: Request) -> Response:
    """写入控制台访问令牌和刷新令牌。"""
    secure = _FORCE_SECURE or _secure_request(request)
    response.set_cookie(
        ACCESS_COOKIE,
        pair["access_token"],
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        httponly=True,
        secure=secure,
        samesite="strict",
        path=_COOKIE_PATH,
    )
    response.set_cookie(
        REFRESH_COOKIE,
        pair["refresh_token"],
        max_age=REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        httponly=True,
        secure=secure,
        samesite="strict",
        path=_COOKIE_PATH,
    )
    return response


def clear_console_cookies(response: Response) -> Response:
    """退出或刷新失败时清理令牌 Cookie。"""
    response.delete_cookie(ACCESS_COOKIE, path=_COOKIE_PATH)
    response.delete_cookie(REFRESH_COOKIE, path=_COOKIE_PATH)
    return response
