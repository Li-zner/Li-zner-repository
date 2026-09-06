/** 聊天态（Pinia）：多会话(localStorage 按人格隔离) + 可取消流式收发 + 评分/删除/重生成 */
import { defineStore } from 'pinia'
import { streamChat } from '../api/chat'
import { ApiError } from '../api/http'
import { fetchPersonas, type Persona } from '../api/personas'
import i18n, { currentLocale } from '../locales'
import { cleanReasoning, toolDisplayName } from '../utils/reasoning'
import { newConversationId, newMessageId, newSessionId, loadSessions, saveSessions } from '../utils/sessions'
import type { ChatSession } from '../utils/sessions'
import { useAuthStore } from './auth'

/** 受限用户（GitHub 试用）每次回答后静默刷新 profile，额度徽标才能实时反映后端扣减 */
function refreshQuotaBadge(): void {
  const auth = useAuthStore()
  if (auth.user?.quota_limited) void auth.loadProfile().catch(() => {})
}

export interface ToolLine {
  kind: 'tool' | 'result' | 'text'
  text: string
}

export interface ChatMessage {
  /** 稳定 id：v-for key 用（历史数据无此字段，回退索引） */
  id?: string
  role: 'user' | 'assistant'
  content: string
  reasoning?: string
  tools?: ToolLine[]
  /** 附件：files 为展示名，fileIds 为服务端文件 id（重新生成时重发用） */
  files?: string[]
  fileIds?: string[]
  timestamp?: number
  rated?: boolean
  streaming?: boolean
}

export const useChatStore = defineStore('chat', {
  state: () => ({
    sessions: [] as ChatSession[],
    currentSessionId: '',
    personas: [] as Persona[],
    currentPersonaId: 'unified',
    streaming: false,
    error: '',
    /** 地图页「去这里」写入的自动提问 prompt（ChatView 挂载时消费） */
    autoPrompt: '',
    /** 402 触发的绑定手机引导文案（非空时 ChatView 弹绑定弹窗） */
    bindPhoneTip: '',
    /** 流式中止控制器（send while streaming = cancel） */
    abort: null as AbortController | null,
  }),

  getters: {
    currentSession(): ChatSession | undefined {
      return this.sessions.find((s) => s.id === this.currentSessionId) ?? this.sessions[0]
    },
    messages(): ChatMessage[] {
      return this.currentSession?.messages ?? []
    },
    /** 置顶会话浮到最前（稳定排序，组内保持原顺序） */
    sortedSessions(): ChatSession[] {
      return [...this.sessions].sort(
        (a, b) => Number(b.pinned ?? false) - Number(a.pinned ?? false),
      )
    },
  },

  actions: {
    /** 按当前人格加载本地会话；无会话则建一个 */
    initSessions() {
      try {
        this.sessions = loadSessions(this.currentPersonaId)
        if (!this.sessions.length) this.createSession()
        this.currentSessionId = this.sessions[0].id
      } catch (e) {
        console.error('[chat.initSessions] FAILED:', e)  // 会话加载失败需可见
      }
    },

    createSession() {
      const s: ChatSession = {
        id: newSessionId(),
        personaId: this.currentPersonaId,
        title: '新对话',
        conversationId: newConversationId(),
        messages: [],
        updatedAt: Date.now(),
      }
      this.sessions.unshift(s)
      this.currentSessionId = s.id
      this.persist()
    },

    switchSession(id: string) {
      if (this.streaming) return
      this.currentSessionId = id
      this.persist()
    },

    deleteSession(id: string) {
      if (this.sessions.length <= 1) return
      this.sessions = this.sessions.filter((s) => s.id !== id)
      if (this.currentSessionId === id) this.currentSessionId = this.sessions[0].id
      this.persist()
    },

    renameSession(id: string, title: string) {
      const s = this.sessions.find((x) => x.id === id)
      if (s && title.trim()) {
        s.title = title.trim()
        this.persist()
      }
    },

    /** 置顶/取消置顶会话 */
    togglePin(id: string) {
      const s = this.sessions.find((x) => x.id === id)
      if (!s) return
      s.pinned = !s.pinned
      this.persist()
    },

    persist() {
      saveSessions(this.currentPersonaId, this.sessions)
    },

    async loadPersonas() {
      const resp = await fetchPersonas()
      this.personas = resp.personas
      if (!this.personas.some((p) => p.id === this.currentPersonaId)) {
        this.currentPersonaId = resp.current || this.personas[0]?.id || 'unified'
      }
      this.initSessions()
    },

    /** 切人格：重载该人格的本地会话（旧版 travel/me 双模式惯例的泛化） */
    switchPersona(personaId: string) {
      if (this.streaming || personaId === this.currentPersonaId) return
      this.currentPersonaId = personaId
      this.initSessions()
    },

    newConversation() {
      this.createSession()
    },

    setAutoPrompt(prompt: string) {
      this.autoPrompt = prompt
    },

    /** 取消当前流式生成（AbortController） */
    cancelStream() {
      this.abort?.abort()
    },

    /** 删除某条问答（assistant 及其前面的 user 消息，本地删除） */
    deleteQA(messageIndex: number): void {
      const session = this.currentSession
      if (!session || messageIndex < 1) return
      session.messages.splice(messageIndex - 1, 2)
      this.persist()
    },

    /** 重新生成：移除该问答对后连附件一起重发原问题 */
    async regenerate(messageIndex: number): Promise<void> {
      const session = this.currentSession
      if (!session || this.streaming || messageIndex < 1) return
      const userMsg = session.messages[messageIndex - 1]
      const query = userMsg?.role === 'user' ? userMsg.content : ''
      if (!query) return
      const fileIds = userMsg.fileIds ?? []
      session.messages.splice(messageIndex - 1, 2)
      this.persist()
      await this.send(query, fileIds)
    },

    /** 评分后标记该回复已评（持久化，刷新后不再显示评分按钮） */
    markRated(messageIndex: number): void {
      const msg = this.currentSession?.messages[messageIndex]
      if (msg) {
        msg.rated = true
        this.persist()
      }
    },

    /** 发送一条消息：流式期间再次点击发送按钮 = 取消 */
    async send(query: string, fileIds: string[] = []): Promise<void> {
      const tr = (key: string, params?: Record<string, unknown>) =>
        i18n.global.t(key, params ?? {})
      if (this.streaming) {
        this.cancelStream()
        return
      }
      const session = this.currentSession
      if (!session || (!query.trim() && !fileIds.length)) return

      // 首条消息生成会话标题（旧版惯例：前 30 字）
      if (!session.messages.length) {
        session.title = query.slice(0, 30) + (query.length > 30 ? '…' : '')
      }

      const sendQuery = query || tr('send_file_query')
      const displayText = query || tr('send_file_display')
      const userMsg: ChatMessage = { id: newMessageId(), role: 'user', content: displayText, timestamp: Date.now() }
      if (fileIds.length) userMsg.fileIds = fileIds
      session.messages.push(userMsg)
      const assistant: ChatMessage = {
        id: newMessageId(),
        role: 'assistant',
        content: '',
        reasoning: '',
        tools: [],
        timestamp: Date.now(),
        streaming: true,
      }
      session.messages.push(assistant)
      this.streaming = true
      this.error = ''

      const abort = new AbortController()
      this.abort = abort

      try {
        await streamChat({
          query: sendQuery,
          conversationId: session.conversationId,
          personaId: this.currentPersonaId !== 'unified' ? this.currentPersonaId : undefined,
          fileIds,
          lang: currentLocale(),
          signal: abort.signal,
          onEvent: (ev) => {
            const last = session.messages[session.messages.length - 1]
            if (!last) return
            if (ev.type === 'answer_chunk') {
              last.content += String(ev.content ?? '')
            } else if (ev.type === 'answer_complete') {
              // 以后端终检/兜底后的完整内容为准
              const content = typeof ev.content === 'string' ? ev.content : ''
              if (content) last.content = content
              refreshQuotaBadge()
            } else if (ev.type === 'reasoning_chunk') {
              last.reasoning = (last.reasoning ?? '') + cleanReasoning(String(ev.content ?? ''))
            } else if (ev.type === 'thought') {
              const text = String(ev.content ?? '').replace(/[🤔💭]/gu, '').trim()
              if (text) last.tools?.push({ kind: 'text', text })
            } else if (ev.type === 'tool_call') {
              // 工具名转可读名称；未知工具不展示（防暴露内部函数签名，对齐旧版）
              const name = String((ev as { name?: string }).name ?? '')
              const label = toolDisplayName(name)
              if (label === name) return
              const args = (ev as { args?: Record<string, unknown> }).args ?? {}
              const vals = Object.values(args).filter(
                (v) => typeof v === 'string' && String(v).trim(),
              )
              last.tools?.push({
                kind: 'tool',
                text:
                  tr('calling_tool') +
                  label +
                  (vals.length ? `（${vals.join('，')}）` : ''),
              })
            } else if (ev.type === 'tool_result') {
              // 工具失败静默跳过，不给用户负面信号（对齐旧版）
              const result = (ev as { result?: { error?: string; restaurants?: unknown[]; hotels?: unknown[]; route?: unknown } }).result
              if (!result || result.error) return
              let summary = tr('query_done')
              if (result.restaurants) summary = tr('found_restaurants', { n: result.restaurants.length })
              else if (result.hotels) summary = tr('found_hotels', { n: result.hotels.length })
              else if (result.route) summary = tr('route_done')
              last.tools?.push({ kind: 'result', text: tr('result_prefix') + summary })
            }
          },
        })
        // 流结束兜底：空内容给明确提示
        if (!assistant.content) {
          assistant.content = i18n.global.t('no_reply')
        }
      } catch (e) {
        // 用户主动取消：保留已生成内容，不算错误
        if (abort.signal.aborted) {
          assistant.streaming = false
          this.streaming = false
          this.abort = null
          this.persist()
          return
        }
        // 402 = GitHub 试用额度用尽 → 弹绑定手机引导
        if (e instanceof ApiError && e.status === 402) {
          assistant.content = i18n.global.t('quota_exhausted')
          this.bindPhoneTip = i18n.global.t('bind_phone_exhausted_tip')
          refreshQuotaBadge()
        } else {
          this.error = e instanceof Error ? e.message : i18n.global.t('request_failed')
          assistant.content = this.error
        }
      } finally {
        assistant.streaming = false
        this.streaming = false
        this.abort = null
        session.updatedAt = Date.now()
        this.persist()
      }
    },
  },
})
