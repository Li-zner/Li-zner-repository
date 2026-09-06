/**
 * 聊天流式 API：POST /v2/chat/stream（POST-SSE），逐事件回调给调用方
 *
 * 事件协议（见 chat_stream_core/chat_react）：
 *   thought / reasoning_chunk / reasoning_done / answer_chunk /
 *   answer_complete / tool_call / tool_result，终止 data: [DONE]
 */
import { ApiError, getAccessToken, refreshAccessToken } from './http'
import { SseLineFramer, type SseEvent } from './sse'

export type ChatStreamHandler = (event: SseEvent) => void

export interface ChatStreamOptions {
  query: string
  conversationId: string
  personaId?: string
  fileIds?: string[]
  lang?: string
  /** 收到 answer_chunk / answer_complete 时回调（返回 false 可中止消费） */
  onEvent: ChatStreamHandler
  /** 客户端主动中止 */
  signal?: AbortSignal
}

/** 发起流式请求；401 时单飞刷新后重试一次（对齐 http.ts 的 request 语义） */
async function fetchStream(body: string, signal: AbortSignal | undefined, retried: boolean): Promise<Response> {
  const resp = await fetch('/v2/chat/stream', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${getAccessToken()}`,
      Accept: 'text/event-stream',
    },
    body,
    signal,
  })
  if (resp.status === 401 && !retried && (await refreshAccessToken())) {
    return fetchStream(body, signal, true)
  }
  return resp
}

export async function streamChat(opts: ChatStreamOptions): Promise<void> {
  const body = JSON.stringify({
    query: opts.query,
    user: opts.conversationId, // 后端 ChatRequest 兼容字段（鉴权以 JWT 为准）
    conversation_id: opts.conversationId,
    persona_id: opts.personaId || null,
    file_ids: opts.fileIds?.length ? opts.fileIds : null,
    lang: opts.lang ?? 'zh',
  })

  const resp = await fetchStream(body, opts.signal, false)
  if (!resp.ok || !resp.body) {
    let detail = `HTTP ${resp.status}`
    try {
      const err = await resp.json()
      detail = err.detail ?? detail
    } catch {
      /* 非 JSON 错误体 */
    }
    // 带状态码抛出：store 依赖 ApiError.status===402 触发绑手机引导
    throw new ApiError(resp.status, detail)
  }

  // 完整事件对象透传：tool_call 的 name/args、tool_result 的 result 是顶层字段，
  // 只传 type/content 会把工具动态展示整个剥掉
  const framer = new SseLineFramer((event) => {
    if (event) opts.onEvent(event)
  })

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    framer.feed(decoder.decode(value, { stream: true }))
  }
  framer.flush()
}
