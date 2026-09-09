/** ============================================================
 *  可复用资产：http-jwt-client.ts
 *  来源：agent_gateway/frontend/src/api/http.ts
 *  实战验证：生产运行中（含 09-06 修复：显式 Authorization 不被覆盖）
 *  依赖：零依赖（fetch/localStorage）
 *  提取：2026-09-06；二次复用后请在来源行补注项目名
 *  ============================================================ */
/**
 * HTTP 客户端：JWT 注入 / 401 刷新重试 / 统一错误
 *
 * 认证态说明：token 存 localStorage（演示部署形态）；若升级生产形态应改为
 * httpOnly Cookie + 后端会话，避免 XSS 读取面。401 时先用 refresh token 换新
 * （/api/refresh，轮换语义），成功则重放原请求一次，失败则清除登录态。
 */
const BASE = ''

export interface TokenPair {
  access_token: string
  refresh_token: string
  token_type: string
}

const ACCESS_KEY = 'gw_access_token'
const REFRESH_KEY = 'gw_refresh_token'

export function getAccessToken(): string {
  return localStorage.getItem(ACCESS_KEY) ?? ''
}

export function getRefreshToken(): string {
  return localStorage.getItem(REFRESH_KEY) ?? ''
}

export function saveTokens(pair: TokenPair): void {
  localStorage.setItem(ACCESS_KEY, pair.access_token)
  localStorage.setItem(REFRESH_KEY, pair.refresh_token)
}

export function clearTokens(): void {
  localStorage.removeItem(ACCESS_KEY)
  localStorage.removeItem(REFRESH_KEY)
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message)
  }
}

let onUnauthorized: (() => void) | null = null

/** 注册 401 最终兜底回调（router 守卫跳登录页），避免 http 层反向依赖 router */
export function setUnauthorizedHandler(fn: () => void): void {
  onUnauthorized = fn
}

let refreshInFlight: Promise<boolean> | null = null

async function tryRefresh(): Promise<boolean> {
  // 单飞：并发 401 只发起一次刷新（refresh token 是轮换语义，重复用会互踩失效）
  if (refreshInFlight) return refreshInFlight
  refreshInFlight = doRefresh()
  const ok = await refreshInFlight
  refreshInFlight = null
  return ok
}

async function doRefresh(): Promise<boolean> {
  const refresh = getRefreshToken()
  if (!refresh) return false
  try {
    const resp = await fetch(`${BASE}/api/refresh`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${refresh}` },
    })
    if (!resp.ok) return false
    const pair = (await resp.json()) as TokenPair
    // 响应形状防御：缺字段视为刷新失败（避免把 undefined 存成 token）
    if (!pair?.access_token || !pair?.refresh_token) return false
    saveTokens(pair)
    return true
  } catch {
    return false
  }
}

/** 401 单飞刷新（供 SSE 等绕过 request() 的裸 fetch 复用） */
export function refreshAccessToken(): Promise<boolean> {
  return tryRefresh()
}

async function doFetch(url: string, init: RequestInit, retried: boolean): Promise<Response> {
  const headers = new Headers(init.headers)
  const token = getAccessToken()
  // 已显式指定 Authorization 时不覆盖（如 logout 携 refresh token 供后端吊销）
  if (token && !headers.has('Authorization')) headers.set('Authorization', `Bearer ${token}`)
  const resp = await fetch(url, { ...init, headers })
  if (resp.status === 401 && !retried) {
    if (await tryRefresh()) {
      return doFetch(url, init, true)
    }
    clearTokens()
    onUnauthorized?.()
  }
  return resp
}

/** 统一 GET/POST：非 2xx 抛 ApiError（detail 优先，兼容 FastAPI 错误体） */
export async function request<T>(url: string, init: RequestInit = {}): Promise<T> {
  const resp = await doFetch(url, init, false)
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`
    try {
      const body = await resp.json()
      detail = body.detail ?? body.error ?? detail
    } catch {
      /* 非 JSON 错误体，保留状态码文案 */
    }
    throw new ApiError(resp.status, detail)
  }
  return (await resp.json()) as T
}

export function get<T>(url: string): Promise<T> {
  return request<T>(url)
}

export function post<T>(url: string, body?: unknown, headers?: Record<string, string>): Promise<T> {
  return request<T>(url, {
    method: 'POST',
    headers: {
      ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      ...headers,
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
}

/** 文件 multipart 上传 */
export function upload<T>(url: string, file: File): Promise<T> {
  const form = new FormData()
  form.append('file', file)
  return request<T>(url, { method: 'POST', body: form })
}
