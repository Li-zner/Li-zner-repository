"""控制台安全响应头：限制脚本、框架、来源和浏览器能力。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from starlette.responses import Response

from .config import TRUST_PROXY_HEADERS, peer_is_trusted_proxy


SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}

# 网关主站（JSON API + 自建前端 SPA，由 main.py StaticFiles 挂载、nginx 反代到本进程，
# 所以这些头会真的落到浏览器）与控制台的差异只有两处，均是对 frontend/src 的实测结论：
#   - connect-src：唯一外部数据源是行政区划边界 geo.datav.aliyun.com（utils/geo.ts:4）
#   - Permissions-Policy：前端用浏览器定位（composables/useUserLocation.ts:54）
# 其余面已核实为同源：构建产物 static/index.html 只有 1 个外链 <script>（无内联脚本），
# 全仓无 iframe、无 new Worker，故 script-src 'self' 与 frame-ancestors 'none' 可直接沿用。
# 用 replace 而非重写整串：两处一旦与基线漂移，单测里的字面量断言会失败。
GATEWAY_SECURITY_HEADERS = {
    **SECURITY_HEADERS,
    "Content-Security-Policy": SECURITY_HEADERS["Content-Security-Policy"].replace(
        "connect-src 'self';",
        "connect-src 'self' https://geo.datav.aliyun.com;",
    ),
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(self)",
}


def install_security_headers(app: FastAPI,
                             headers: dict[str, str] = SECURITY_HEADERS) -> None:
    """安装统一安全头中间件；默认控制台策略，主站传 GATEWAY_SECURITY_HEADERS。"""

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        return apply_security_headers(request, response, headers)


def apply_security_headers(request: Request, response: Response,
                           headers: dict[str, str] = SECURITY_HEADERS) -> Response:
    """把安全头写入响应，HTTPS 场景额外启用 HSTS。"""
    for name, value in headers.items():
        response.headers[name] = value
    # x-forwarded-proto 是客户端可自报的头（直连网关即可，无需反代），而 HSTS 一旦
    # 下发就是浏览器侧一年期"只用 HTTPS"硬锁：伪造一次该头能让明文站点把用户钉死
    # 在打不开的页面上。故与 auth._client_ip 用同一判据——只有 socket 对端确实命中
    # TRUSTED_PROXY_CIDRS（或显式开了逃生口）才采信转发头，否则只看请求 URL 的 scheme。
    forwarded = ""
    peer = request.client.host if request.client else ""
    if TRUST_PROXY_HEADERS or peer_is_trusted_proxy(peer):
        forwarded = request.headers.get("x-forwarded-proto", "")
    if request.url.scheme == "https" or forwarded.lower() == "https":
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
    return response
