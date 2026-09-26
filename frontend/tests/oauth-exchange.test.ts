/** OAuth 一次性授权码前端兑换协议回归。 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { exchangeOAuthCode } from '../src/api/auth'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('exchangeOAuthCode', () => {
  it('POST 授权码并返回令牌对', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        access_token: 'a1',
        refresh_token: 'r1',
        token_type: 'bearer',
      }),
    } as Response)
    vi.stubGlobal('fetch', fetchMock)

    const pair = await exchangeOAuthCode('one-time-code')

    expect(pair.access_token).toBe('a1')
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/oauth/exchange',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ code: 'one-time-code' }),
      }),
    )
  })
})
