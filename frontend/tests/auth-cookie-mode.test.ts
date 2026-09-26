/** 独立控制台 Cookie 认证回归：不读 localStorage，刷新也不发 Bearer。 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import {
  clearTokens,
  hasAuthSession,
  request,
  saveTokens,
  setAuthMode,
  setUnauthorizedHandler,
} from '../src/api/http'
import { StorageKey } from '../src/enums'
import { useAuthStore } from '../src/stores/auth'

afterEach(() => {
  setActivePinia(createPinia())
  setAuthMode('bearer')
  setUnauthorizedHandler(() => {})
  localStorage.clear()
  vi.unstubAllGlobals()
})

describe('控制台 Cookie 认证', () => {
  it('会话标记不保存令牌', () => {
    setAuthMode('cookie')
    saveTokens({
      access_token: 'sk-example-access',
      refresh_token: 'sk-example-refresh',
      token_type: 'bearer',
    })

    expect(hasAuthSession()).toBe(true)
    expect(localStorage.getItem(StorageKey.AccessToken)).toBeNull()
    expect(localStorage.getItem(StorageKey.RefreshToken)).toBeNull()
    clearTokens()
    expect(hasAuthSession()).toBe(false)
  })

  it('请求不注入本地 Bearer 令牌', async () => {
    localStorage.setItem(StorageKey.AccessToken, 'legacy-access')
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ username: 'admin', role: 'admin' }),
    } as Response)
    vi.stubGlobal('fetch', fetchMock)
    setAuthMode('cookie')

    await request('/api/user/profile')

    const init = fetchMock.mock.calls[0][1] as RequestInit
    const headers = new Headers(init.headers)
    expect(headers.has('Authorization')).toBe(false)
    expect(init.credentials).toBe('same-origin')
  })

  it('401 后通过 Cookie 刷新并重放请求', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({
        ok: false,
        status: 401,
        json: async () => ({ detail: 'expired' }),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({
          access_token: 'new-access',
          refresh_token: 'new-refresh',
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ ok: true }),
      })
    vi.stubGlobal('fetch', fetchMock)
    setAuthMode('cookie')

    await request('/api/user/profile')

    expect(fetchMock.mock.calls[1][0]).toBe('/api/refresh')
    const refreshInit = fetchMock.mock.calls[1][1] as RequestInit
    expect(new Headers(refreshInit.headers).has('Authorization')).toBe(false)
    expect(localStorage.getItem(StorageKey.AccessToken)).toBeNull()
  })

  it('Cookie 模式登出仍请求服务端清 Cookie', async () => {
    setActivePinia(createPinia())
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ success: true }),
    } as Response)
    vi.stubGlobal('fetch', fetchMock)
    setAuthMode('cookie')
    saveTokens({
      access_token: 'ignored',
      refresh_token: 'ignored',
      token_type: 'bearer',
    })

    await useAuthStore().logout()

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/logout',
      expect.objectContaining({ method: 'POST', credentials: 'same-origin' }),
    )
    expect(hasAuthSession()).toBe(false)
  })

  it('刷新重试后再次 401 仍触发全局登出兜底', async () => {
    const unauthorized = vi.fn()
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: false, status: 401 } as Response)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ access_token: 'a2', refresh_token: 'r2' }),
      } as Response)
      .mockResolvedValueOnce({ ok: false, status: 401 } as Response)
    vi.stubGlobal('fetch', fetchMock)
    setAuthMode('cookie')
    saveTokens({
      access_token: 'ignored',
      refresh_token: 'ignored',
      token_type: 'bearer',
    })
    setUnauthorizedHandler(unauthorized)

    await expect(request('/api/user/profile')).rejects.toMatchObject({ status: 401 })
    expect(unauthorized).toHaveBeenCalledTimes(1)
    expect(hasAuthSession()).toBe(false)
  })
})
