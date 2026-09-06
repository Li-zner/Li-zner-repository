// streamChat 事件透传回归检查：
// 1) tool_call 的 name/args、tool_result 的 result 是 SSE 顶层字段，曾被错误剥掉导致工具动态不显示
// 2) 401 时应刷新令牌并重试一次（对齐 http.ts request 语义）
import { afterEach, describe, expect, it, vi } from 'vitest'
import { streamChat } from '../src/api/chat'

const encoder = new TextEncoder()

/** 伪 Response：按行喂 chunk，避免依赖 jsdom 的 fetch/ReadableStream 实现 */
function sseResponse(lines: string[]): Response {
  const chunks = lines.map((l) => encoder.encode(l))
  let i = 0
  const body = {
    getReader: () => ({
      read: async () =>
        i < chunks.length ? { done: false, value: chunks[i++] } : { done: true, value: undefined },
    }),
  }
  return { ok: true, status: 200, body } as unknown as Response
}

function json401(): Response {
  return { ok: false, status: 401, body: null, json: async () => ({ detail: 'unauthorized' }) } as unknown as Response
}

function jsonStatus(status: number, detail: string): Response {
  return { ok: false, status, body: null, json: async () => ({ detail }) } as unknown as Response
}

afterEach(() => {
  vi.unstubAllGlobals()
  localStorage.clear()
})

describe('streamChat 事件透传', () => {
  it('tool_call/tool_result 顶层字段完整到达 onEvent', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(sseResponse([
      'data: {"type": "tool_call", "name": "query_weather", "args": {"city": "北京"}}\n\n',
      'data: {"type": "tool_result", "index": 0, "result": {"temperature": "25"}}\n\n',
      'data: [DONE]\n\n',
    ])))
    const events: Record<string, unknown>[] = []
    await streamChat({
      query: 'q',
      conversationId: 'c',
      onEvent: (ev) => events.push(ev as Record<string, unknown>),
    })
    const toolCall = events.find((e) => e.type === 'tool_call')
    const toolResult = events.find((e) => e.type === 'tool_result')
    expect(toolCall?.name).toBe('query_weather')
    expect((toolCall?.args as Record<string, string>)?.city).toBe('北京')
    expect((toolResult?.result as Record<string, string>)?.temperature).toBe('25')
  })

  it('401 时刷新令牌后重试一次', async () => {
    localStorage.setItem('gw_refresh_token', 'r1')
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(json401())
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ access_token: 'a1', refresh_token: 'r2', token_type: 'bearer' }),
      } as unknown as Response)
      .mockResolvedValueOnce(sseResponse(['data: [DONE]\n\n']))
    vi.stubGlobal('fetch', fetchMock)
    await streamChat({ query: 'q', conversationId: 'c', onEvent: () => {} })
    expect(fetchMock).toHaveBeenCalledTimes(3)
    expect(localStorage.getItem('gw_access_token')).toBe('a1')
  })

  it('402 抛 ApiError 且带状态码（配额用尽→绑手机引导依赖此判断）', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonStatus(402, '免费额度已用完')))
    await expect(
      streamChat({ query: 'q', conversationId: 'c', onEvent: () => {} }),
    ).rejects.toMatchObject({ status: 402, message: '免费额度已用完' })
  })
})
