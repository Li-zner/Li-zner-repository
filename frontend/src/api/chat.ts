/**
 * 聊天流式 API：POST /v2/chat/stream（POST-SSE），逐事件回调给调用方
 *
 * 事件协议（见 chat_stream_core/chat_react）：
 *   thought / reasoning_chunk / reasoning_done / answer_chunk /
 *   answer_complete / tool_call / tool_result，终止 data: [DONE]
 */
import { ApiError, getAccessToken, notifyUnauthorized, refreshAccessToken, resolve_url } from './http'
import { SseLineFramer, SSE_DONE, type SseEvent } from './sse'

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
  const resp = await fetch(resolve_url('/v2/chat/stream'), {
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
  // 刷新最终失败：与 http.ts request 语义对齐——清登录态并触发跳登录兜底
  // （2026-09-10 审查 P2：原先流式 401 刷新失败既不清 token 也不跳登录）
  if (resp.status === 401) {
    notifyUnauthorized()
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
  // 截断检测（2026-09-12 外部复核 P1）：后端所有路径都以 answer_complete+[DONE]
  // 终结（chat_fallback 兜底保证）；两者皆缺 = 流被中途掐断，回答可能不完整
  let terminated = false
  const framer = new SseLineFramer((event) => {
    if (!event) return
    if (event === SSE_DONE) {
      terminated = true
      return
    }
    if (event.type === 'answer_complete') terminated = true
    opts.onEvent(event)
  })

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  // 空闲看门狗：60s 无任何字节视为连接僵死（2026-09-12 修复：原先流悬挂时
  // 前端永久停留"生成中"且无法恢复）
  let timedOut = false
  let watchdog: ReturnType<typeof setTimeout> | null = null
  const resetWatchdog = () => {
    if (watchdog) clearTimeout(watchdog)
    watchdog = setTimeout(() => {
      timedOut = true
      reader.cancel('idle timeout').catch(() => {})
    }, 60000)
  }
  resetWatchdog()
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      // 每个有效 chunk 都代表连接仍在工作，必须重新计算空闲时间。
      resetWatchdog()
      framer.feed(decoder.decode(value, { stream: true }))
    }
  } catch (e) {
    if (!timedOut) throw e
    throw new ApiError(504, '连接空闲超时，请重试')
  } finally {
    if (watchdog) clearTimeout(watchdog)
  }
  framer.flush()
  if (!terminated) {
    throw new ApiError(502, '连接中断，回答可能不完整，请重试')
  }
}
