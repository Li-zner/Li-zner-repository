<script setup lang="ts">
/** RAG 运维会话：左边上下文，中间对话，右边实时状态。 */
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  label_retrieval_method,
  label_route,
  label_severity,
  label_stage,
  label_status,
} from '../utils/ragLabels'
import {
  acknowledgeIncident,
  approveRagAction,
  cancelRagAction,
  getRagIncident,
  getRagRequest,
  getRagStatus,
  listRagChangeSets,
  listRagIncidents,
  listRagRequests,
  planRagAction,
  proposeRagChangeSet,
  resolveIncident,
  streamRagOpsAsk,
  type RagChangeSet,
  type RagIncident,
  type RagRequest,
  type RagRequestContent,
  type RagSpan,
  type RagStatus,
} from '../api/ragAdmin'
import { useAuthStore } from '../stores/auth'
import { renderOpsMarkdown } from '../utils/opsMarkdown'

interface OpsMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  createdAt: number
}

const router = useRouter()
const auth = useAuthStore()
const status = ref<RagStatus | null>(null)
const incidents = ref<RagIncident[]>([])
const requests = ref<RagRequest[]>([])
const selectedIncident = ref<RagIncident | null>(null)
type RagRequestDetail = RagRequest & {
  spans: RagSpan[]
  content: RagRequestContent
}

const selectedRequest = ref<RagRequestDetail | null>(null)
const requestLoading = ref(false)
const requestError = ref('')
const changeSets = ref<RagChangeSet[]>([])
const csBusy = ref(false)
const messages = ref<OpsMessage[]>([])
const input = ref('')
const loading = ref(true)
const sending = ref(false)
const forbidden = ref(false)
const error = ref('')
const transcriptEl = ref<HTMLElement | null>(null)
let refreshTimer: number | undefined
const requestCache = new Map<string, RagRequestDetail>()
let requestSeq = 0

function storageKeyFor(owner = ''): string {
  return `rag_ops_chat:${owner || 'anonymous'}`
}

const storageKey = computed(() => storageKeyFor(auth.user?.username ?? ''))
const activeIncidents = computed(
  () => incidents.value.filter((item) => !['resolved', 'closed'].includes(item.status)),
)
const pendingChangeSets = computed(() =>
  changeSets.value.filter((item) => item.status === 'pending_approval'),
)
const quickPrompts = [
  '总结当前异常并判断严重程度',
  '最近一次请求卡在哪个阶段',
  '给出是否需要人工介入的结论',
  '推荐一个安全且可验证的修复动作',
]

function uid(): string {
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function persistMessages(owner = ''): void {
  localStorage.setItem(
    storageKeyFor(owner || auth.user?.username || ''),
    JSON.stringify(messages.value.slice(-40)),
  )
}

function loadMessages() {
  try {
    const raw = localStorage.getItem(storageKey.value)
    messages.value = raw ? JSON.parse(raw) : []
  } catch {
    messages.value = []
  }
  if (!messages.value.length) {
    messages.value.push({
      id: uid(),
      role: 'assistant',
      content: '我是 RAG 运维助手。选择左侧 incident 后，可以直接询问根因、影响范围和修复建议。',
      createdAt: Date.now(),
    })
  }
}

async function scrollBottom() {
  await nextTick()
  const el = transcriptEl.value
  if (el) el.scrollTop = el.scrollHeight
}

async function refresh() {
  const [summary, incidentList, requestList, changeSetList] = await Promise.all([
    getRagStatus(),
    listRagIncidents(40),
    listRagRequests(20),
    listRagChangeSets(20).catch(() => ({ items: [] as RagChangeSet[] })),
  ])
  status.value = summary
  incidents.value = incidentList.items
  requests.value = requestList.items
  changeSets.value = changeSetList.items
  // 刷新成功即清除历史错误：否则一次失败后 UI 永久卡在错误页，
  // 15s 轮询即使恢复成功用户也看不到（对照组 RagConsoleView 同款）
  error.value = ''
  if (!selectedIncident.value && activeIncidents.value.length) {
    await selectIncident(activeIncidents.value[0].incident_id)
  }
}

async function selectIncident(incidentId: number) {
  try {
    selectedIncident.value = await getRagIncident(incidentId)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载事件详情失败'
  }
}

function fmtTime(value?: string | null): string {
  return value ? value.slice(5, 16).replace('T', ' ') : '-'
}


/** 格式化阶段耗时，空值显示短横线。 */
function fmtMs(value?: number | null): string {
  return value == null ? '-' : `${value}ms`
}


/** 点击请求只读取并缓存详情，不触发模型分析。 */
async function selectRequest(requestUid: string) {
  if (selectedRequest.value?.request_uid === requestUid) return
  const seq = ++requestSeq
  requestError.value = ''
  const cached = requestCache.get(requestUid)
  if (cached) {
    if (seq === requestSeq) selectedRequest.value = cached
    return
  }
  requestLoading.value = true
  try {
    const detail = await getRagRequest(requestUid)
    requestCache.set(requestUid, detail)
    if (seq === requestSeq) selectedRequest.value = detail
  } catch (err) {
    if (seq === requestSeq) {
      selectedRequest.value = null
      requestError.value = err instanceof Error ? err.message : '请求详情加载失败'
    }
  } finally {
    if (seq === requestSeq) requestLoading.value = false
  }
}


/** 构造包含已读取阶段数据的问题，避免模型误判请求不存在。 */
function requestAnalysisQuestion(): string {
  const item = selectedRequest.value
  if (!item) return ''
  const spans = item.spans.slice(-50).map((span) => [
    `stage=${span.stage}`,
    `round=${span.round_no}`,
    `status=${span.status}`,
    `latency_ms=${span.latency_ms ?? '-'}`,
    `result_count=${span.result_count ?? '-'}`,
    `error_code=${span.error_code || '-'}`,
  ].join(' '))
  const chunk_lines = item.content.chunks.slice(0, 8).map((chunk) => [
    `chunk_key=${chunk.chunk_key}`,
    `heading=${chunk.heading || '-'}`,
    `content=${(chunk.content || '').slice(0, 600)}`,
  ].join('; '))
  return [
    `请分析请求 ${item.request_uid} 的执行链路。`,
    '以下是已从请求级 trace 读取的结构化数据，不要声称该请求不存在：',
    `route=${item.route || '-'}; status=${item.status}`,
    `model=${item.selected_model || '-'}; provider=${item.provider || '-'}`,
    `first_token_ms=${item.first_token_ms ?? '-'}; total_ms=${item.total_ms ?? '-'}`,
    `input_tokens=${item.input_tokens ?? '-'}; output_tokens=${item.output_tokens ?? '-'}`,
    `用户问题=${item.content.query || '-'}`,
    `最终回答=${(item.content.answer || '').slice(0, 1200)}`,
    '阶段数据：',
    ...spans,
    '召回内容：',
    ...chunk_lines,
  ].join('\n')
}


/** 只有用户点击按钮时才把选中的请求交给运维助手分析。 */
async function analyzeSelectedRequest() {
  const question = requestAnalysisQuestion()
  if (!question) return
  await send(question)
}


function contextQuestion(question: string): string {
  if (!selectedIncident.value) return question
  const item = selectedIncident.value
  const lines = [
    `当前关注 Incident #${item.incident_id}`,
    `规则 ${item.code}，严重度 ${item.severity}，状态 ${item.status}`,
    `标题 ${item.title}`,
  ]
  // 样本证据随上下文回带（2026-09-14 审计 P2）：原先只带标题/规则/状态，
  // 模型只能依赖全局最近 20 条，目标事件的证据可能在上下文里缺失
  const samples = (item.verdicts || []).slice(0, 3)
  if (samples.length) {
    lines.push('样本证据（含建议修复参数 patch）：')
    for (const sample of samples) {
      lines.push([
        `query=${sample.query_snippet || '-'}`,
        `evidence=${(sample.evidence || []).slice(0, 3).join(' | ') || '-'}`,
        `patch=${sample.patch && Object.keys(sample.patch).length ? JSON.stringify(sample.patch) : '-'}`,
      ].join('; '))
    }
  }
  lines.push(question)
  return lines.join('\n')
}

async function send(question?: string) {
  const text = (question ?? input.value).trim()
  if (!text || sending.value) return
  const history = messages.value.slice(-8).map((message) => ({
    role: message.role,
    content: message.content,
  }))
  const ownerAtSend = auth.user?.username ?? ''
  input.value = ''
  messages.value.push({
    id: uid(), role: 'user', content: text, createdAt: Date.now(),
  })
  await scrollBottom()

  // 流式占位：增量填充同一条助手消息，避免长回答整段白屏等待（P2-1）
  const assistantId = uid()
  messages.value.push({
    id: assistantId, role: 'assistant', content: '', createdAt: Date.now(),
  })
  const patchAssistant = (content: string) => {
    const target = messages.value.find((m) => m.id === assistantId)
    if (target) target.content = content
  }
  persistMessages(ownerAtSend)
  await scrollBottom()

  sending.value = true
  try {
    await streamRagOpsAsk({
      question: contextQuestion(text),
      history,
      onEvent: (event) => {
        const current = messages.value.find((m) => m.id === assistantId)
        const streamed = current?.content ?? ''
        if (event.type === 'answer_chunk') {
          patchAssistant(streamed + String(event.content ?? ''))
          void scrollBottom()
        } else if (event.type === 'answer_complete') {
          // SSE 原始负载的 content 类型是 unknown，须显式收敛为 string，
          // 否则 `?? '' || streamed` 会被推成 {} 类型导致 vue-tsc 编译失败（与 chat.ts 同口径）。
          const finalText = typeof event.content === 'string' ? event.content : ''
          patchAssistant(finalText || streamed)
          void scrollBottom()
        } else if (event.type === 'answer_error') {
          patchAssistant(`查询失败：${event.content ?? '未知错误'}`)
        }
        // thought 只是推理流，运维面板不展示，但保持连接活跃
      },
    })
    // 流式接口不回传 status_snapshot；成功后拉一次，恢复旧 /ask 的即时刷新体验
    void refresh().catch(() => {})
  } catch (err) {
    const msg = `查询失败：${err instanceof Error ? err.message : '未知错误'}`
    const current = messages.value.find((m) => m.id === assistantId)
    if (current && current.content) patchAssistant(`${current.content}\n\n${msg}`)
    else patchAssistant(msg)
  } finally {
    sending.value = false
    persistMessages(ownerAtSend)
    await scrollBottom()
  }
}

async function ackIncident() {
  if (!selectedIncident.value) return
  try {
    await acknowledgeIncident(selectedIncident.value.incident_id)
    await selectIncident(selectedIncident.value.incident_id)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '确认操作失败'
  }
}

async function closeIncident() {
  if (!selectedIncident.value) return
  try {
    await resolveIncident(selectedIncident.value.incident_id)
    await selectIncident(selectedIncident.value.incident_id)
    await refresh()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '关闭操作失败'
  }
}

async function runDiagnosis() {
  if (!selectedIncident.value) return
  try {
    await planRagAction(
      selectedIncident.value.incident_id,
      'rerun_diagnosis',
      {},
      '来自 RAG 运维会话',
    )
  } catch (err) {
    // 动作没建成就不能发"动作已经创建"，否则运维助手基于错误前提作答
    // （2026-09-19 审查 P2-6）
    error.value = err instanceof Error ? err.message : '创建诊断动作失败'
    return
  }
  await send('动作已经创建，请说明接下来如何验证结果')
}

function labelChangeType(changeType: string): string {
  return changeType === 'knowledge_add' ? '知识补充' : '失效标记'
}

function csItemCount(cs: RagChangeSet): number {
  return cs.payload.items?.length ?? cs.payload.chunk_keys?.length ?? 0
}

/** 汇总创建期与应用期两层校验警告，供审批人一眼看到风险。 */
function csWarnings(cs: RagChangeSet): string[] {
  const report = (cs.validation_report ?? {}) as {
    warnings?: string[]
    db?: { warnings?: string[] }
  }
  return [...(report.warnings ?? []), ...(report.db?.warnings ?? [])]
}

async function approveChangeSet(cs: RagChangeSet) {
  if (!cs.latest_action_id || csBusy.value) return
  csBusy.value = true
  try {
    await approveRagAction(cs.latest_action_id)
    await refresh()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '审批操作失败'
  } finally {
    csBusy.value = false
  }
}

async function rejectChangeSet(cs: RagChangeSet) {
  if (!cs.latest_action_id || csBusy.value) return
  csBusy.value = true
  try {
    await cancelRagAction(cs.latest_action_id)
    await refresh()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '驳回操作失败'
  } finally {
    csBusy.value = false
  }
}

const csForm = ref<{
  type: 'knowledge_add' | 'knowledge_invalidate'
  body: string
}>({ type: 'knowledge_add', body: '' })
const csFormError = ref('')

const csPlaceholder = computed(() =>
  csForm.value.type === 'knowledge_add'
    ? '[{"content": "第五百三十三条 ……", "heading": "第五百三十三条", "source": "civil_code"}]'
    : 'civil_0123456789abcdef')

/** 提议载荷解析：补充类收 JSON 数组（运维助手可直接吐），失效类每行一个 key。 */
function parseChangeSetPayload(
  type: 'knowledge_add' | 'knowledge_invalidate',
  body: string,
): Record<string, unknown> {
  if (type === 'knowledge_add') {
    const items = JSON.parse(body)
    if (!Array.isArray(items)) throw new Error('知识补充载荷必须是 JSON 数组')
    return { items }
  }
  const chunkKeys = body.split(/\r?\n/).map((s) => s.trim()).filter(Boolean)
  if (!chunkKeys.length) throw new Error('至少填写一个 chunk_key')
  return { chunk_keys: chunkKeys }
}

async function proposeChangeSet() {
  if (!selectedIncident.value || csBusy.value) return
  let payload: Record<string, unknown>
  try {
    payload = parseChangeSetPayload(csForm.value.type, csForm.value.body)
  } catch (err) {
    csFormError.value = err instanceof Error ? err.message : '载荷格式错误'
    return
  }
  csBusy.value = true
  csFormError.value = ''
  try {
    await proposeRagChangeSet(
      selectedIncident.value.incident_id,
      csForm.value.type,
      payload,
      '来自 RAG 运维会话',
    )
    csForm.value.body = ''
    await refresh()
  } catch (err) {
    csFormError.value = err instanceof Error ? err.message : '变更集创建失败'
  } finally {
    csBusy.value = false
  }
}

function clearConversation(): void {
  // 原先只做 messages=[] + loadMessages()：loadMessages 又从上一次
  // persistMessages 写入的 localStorage 把旧消息整批读回（全仓无 removeItem），
  // "新建会话"成了空操作，上一个事件的问答继续作为 history 喂给模型。
  localStorage.removeItem(storageKey.value)
  messages.value = []
  loadMessages()
  persistMessages()
}

onMounted(async () => {
  if (!auth.profileLoaded) {
    try {
      await auth.loadProfile()
    } catch {
      return
    }
  }
  if (auth.user?.role !== 'admin') {
    forbidden.value = true
    loading.value = false
    return
  }
  loadMessages()
  try {
    await refresh()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
    await scrollBottom()
  }
  refreshTimer = window.setInterval(() => void refresh().catch(() => {}), 15000)
})

onUnmounted(() => {
  if (refreshTimer) window.clearInterval(refreshTimer)
})
</script>

<template>
  <div class="ops-shell">
    <aside class="ops-sidebar">
      <div class="brand">
        <strong>RAG 运维</strong>
        <span class="status-dot" :class="{ online: status }"></span>
      </div>
      <button class="new-chat" @click="clearConversation">新建运维会话</button>

      <div class="section-label">活跃事件</div>
      <div class="context-list">
        <button
          v-for="item in activeIncidents"
          :key="item.incident_id"
          class="context-item"
          :class="{ active: selectedIncident?.incident_id === item.incident_id }"
          @click="selectIncident(item.incident_id)"
        >
          <span class="context-title">{{ item.title || item.code }}</span>
          <span class="context-meta">
            {{ label_severity(item.severity) }} ·
            {{ label_status(item.status) }} ·
            {{ item.occurrence_count }} 次
          </span>
        </button>
        <div v-if="!activeIncidents.length" class="empty">暂无活跃事件</div>
      </div>

      <div class="section-label">最近请求</div>
      <div class="request-list">
        <button
          v-for="item in requests.slice(0, 8)"
          :key="item.request_uid"
          class="request-item"
          :class="{ active: selectedRequest?.request_uid === item.request_uid }"
          :title="`查看请求 ${item.request_uid}`"
          @click="selectRequest(item.request_uid)"
        >
          <span>{{ label_route(item.route) }}</span>
          <small>{{ fmtTime(item.created_at) }} · {{ label_status(item.status) }}</small>
        </button>
      </div>
    </aside>

    <main class="ops-chat">
      <header class="ops-header">
        <div>
          <h1>RAG 运维助手</h1>
          <p v-if="selectedIncident">
            当前上下文：事件 #{{ selectedIncident.incident_id }} · {{ selectedIncident.title }}
          </p>
          <p v-else>未选择 incident，将基于全局监测数据回答</p>
        </div>
        <div class="header-actions">
          <button @click="router.push('/admin/rag')">诊断台</button>
        </div>
      </header>

      <section v-if="forbidden" class="state">当前账号没有 RAG 管理权限。</section>
      <section v-else-if="loading" class="state">正在加载 RAG 上下文...</section>
      <section v-else-if="error" class="state danger">{{ error }}</section>

      <template v-else>
        <div ref="transcriptEl" class="transcript">
          <article
            v-for="message in messages"
            :key="message.id"
            class="ops-message"
            :class="message.role"
          >
            <div class="role">{{ message.role === 'user' ? '我' : '运维助手' }}</div>
            <div
              v-if="message.role === 'assistant'"
              class="message-body markdown"
              v-html="renderOpsMarkdown(message.content)"
            ></div>
            <div v-else class="message-body">{{ message.content }}</div>
          </article>
          <article v-if="sending" class="ops-message assistant">
            <div class="role">运维助手</div>
            <div class="message-body thinking">正在分析监测数据...</div>
          </article>
        </div>

        <div v-if="messages.length < 3" class="quick-prompts">
          <button v-for="prompt in quickPrompts" :key="prompt" @click="send(prompt)">
            {{ prompt }}
          </button>
        </div>

        <footer class="composer">
          <textarea
            v-model="input"
            rows="1"
            placeholder="询问异常、根因、影响范围或修复动作"
            @keydown.enter.exact.prevent="send()"
          ></textarea>
          <button :disabled="sending || !input.trim()" @click="send()">
            {{ sending ? '分析中' : '发送' }}
          </button>
        </footer>
      </template>
    </main>

    <aside v-if="status" class="ops-insight">
      <section>
        <h2>实时状态</h2>
        <dl class="metric-list">
          <div><dt>请求链路</dt><dd>{{ status.requests_24h.total }}</dd></div>
          <div><dt>失败请求</dt><dd>{{ status.requests_24h.failed }}</dd></div>
          <div><dt>端到端 95 分位耗时</dt><dd>{{ status.requests_24h.p95_latency_ms }}ms</dd></div>
          <div><dt>活跃事件</dt><dd>{{ activeIncidents.length }}</dd></div>
          <div><dt>待审批动作</dt><dd>{{ status.health_meta.waiting_actions }}</dd></div>
          <div><dt>链路延迟</dt><dd>{{ status.health_meta.trace_lag_seconds ?? '-' }}s</dd></div>
        </dl>
      </section>

      <section v-if="pendingChangeSets.length" class="changeset-context">
        <h2>待审批知识变更</h2>
        <article
          v-for="cs in pendingChangeSets"
          :key="cs.change_set_id"
          class="changeset-card"
        >
          <header class="changeset-heading">
            <strong>#{{ cs.change_set_id }} · {{ labelChangeType(cs.change_type) }}</strong>
            <small>{{ csItemCount(cs) }} 条 · {{ cs.created_by || '-' }} · {{ fmtTime(cs.created_at) }}</small>
          </header>
          <div class="changeset-diff">
            <div
              v-for="item in cs.payload.items ?? []"
              :key="item.chunk_key"
              class="changeset-item"
            >
              <span class="diff-sign add">+</span>
              <div>
                <strong>{{ item.heading || item.chunk_key }}</strong>
                <p>{{ item.content.slice(0, 80) }}</p>
              </div>
            </div>
            <div
              v-for="key in cs.payload.chunk_keys ?? []"
              :key="key"
              class="changeset-item"
            >
              <span class="diff-sign del">−</span>
              <div>
                <strong>{{ key }}</strong>
                <p>标记为失效，检索不再返回该块</p>
              </div>
            </div>
          </div>
          <p v-if="csWarnings(cs).length" class="changeset-warn">
            {{ csWarnings(cs).join('；') }}
          </p>
          <div class="context-actions">
            <button
              :disabled="csBusy || cs.latest_action_status !== 'waiting_approval'"
              @click="approveChangeSet(cs)"
            >批准执行</button>
            <button
              class="danger"
              :disabled="csBusy || cs.latest_action_status !== 'waiting_approval'"
              @click="rejectChangeSet(cs)"
            >驳回</button>
          </div>
        </article>
      </section>

      <section
        v-if="selectedIncident && !['resolved', 'closed'].includes(selectedIncident.status)"
        class="changeset-propose"
      >
        <h2>提议知识变更</h2>
        <select v-model="csForm.type" :disabled="csBusy">
          <option value="knowledge_add">知识补充（JSON 数组）</option>
          <option value="knowledge_invalidate">失效标记（每行一个 chunk_key）</option>
        </select>
        <textarea
          v-model="csForm.body"
          rows="3"
          :disabled="csBusy"
          :placeholder="csPlaceholder"
        ></textarea>
        <p v-if="csFormError" class="changeset-warn">{{ csFormError }}</p>
        <div class="context-actions">
          <button :disabled="csBusy || !csForm.body.trim()" @click="proposeChangeSet">
            {{ csBusy ? '提交中' : '提交待审批变更' }}
          </button>
        </div>
      </section>

      <section v-if="requestLoading" class="request-context">
        <h2>请求上下文</h2>
        <p class="empty">正在读取请求阶段数据...</p>
      </section>

      <section v-else-if="requestError" class="request-context">
        <h2>请求上下文</h2>
        <p class="error-text">{{ requestError }}</p>
      </section>

      <section v-else-if="selectedRequest" class="request-context">
        <div class="request-heading">
          <h2>请求上下文</h2>
          <button class="text-button" @click="selectedRequest = null">清除</button>
        </div>
        <p class="request-uid">{{ selectedRequest.request_uid }}</p>
        <dl class="request-meta">
          <div><dt>状态</dt><dd>{{ label_status(selectedRequest.status) }}</dd></div>
          <div><dt>路径</dt><dd>{{ label_route(selectedRequest.route) }}</dd></div>
          <div><dt>模型</dt><dd>{{ selectedRequest.selected_model || '-' }}</dd></div>
          <div>
            <dt>首包 / 总耗时</dt>
            <dd>{{ fmtMs(selectedRequest.first_token_ms) }} / {{ fmtMs(selectedRequest.total_ms) }}</dd>
          </div>
        </dl>

        <div class="content-block">
          <span class="content-label">用户问题</span>
          <p class="content-text">{{ selectedRequest.content.query || '未记录' }}</p>
        </div>

        <details class="content-detail" open>
          <summary>最终回答 <span>{{ selectedRequest.content.answer_len }} 字</span></summary>
          <pre class="content-pre">{{ selectedRequest.content.answer || '未记录' }}</pre>
        </details>

        <details class="content-detail">
          <summary>召回内容 <span>{{ selectedRequest.content.chunks.length }} 块</span></summary>
          <div
            v-for="run in selectedRequest.content.retrieval_runs"
            :key="run.created_at + run.method"
            class="retrieval-run"
          >
            <span>{{ label_retrieval_method(run.method) }}</span>
            <span>{{ run.returned_count ?? 0 }} 条 · {{ fmtMs(run.latency_ms) }}</span>
          </div>
          <article
            v-for="chunk in selectedRequest.content.chunks"
            :key="chunk.chunk_key"
            class="chunk-item"
          >
            <strong>{{ chunk.heading || chunk.chunk_key }}</strong>
            <p>{{ chunk.content }}</p>
          </article>
          <p v-if="!selectedRequest.content.chunks.length" class="empty">
            未记录可展示的知识片段。
          </p>
        </details>

        <details class="content-detail">
          <summary>执行阶段 <span>{{ selectedRequest.spans.length }} 个</span></summary>
          <div class="span-list">
            <div v-for="span in selectedRequest.spans" :key="span.span_uid" class="span-row">
              <span>{{ label_stage(span.stage) }}<small v-if="span.round_no > 1"> #{{ span.round_no }}</small></span>
              <span>{{ label_status(span.status) }}</span>
              <span>{{ fmtMs(span.latency_ms) }}</span>
            </div>
            <p v-if="!selectedRequest.spans.length" class="empty">该请求没有阶段 span。</p>
          </div>
        </details>
        <button class="analyze-request" :disabled="sending" @click="analyzeSelectedRequest">
          {{ sending ? '分析中' : '分析此请求' }}
        </button>
      </section>

      <section v-if="selectedIncident" class="incident-context">
        <h2>事件上下文</h2>
        <p class="incident-title">{{ selectedIncident.title }}</p>
        <p>{{ selectedIncident.suspected_cause || '暂无根因说明' }}</p>
        <p class="recommendation">{{ selectedIncident.recommended_action || '暂无建议动作' }}</p>
        <div class="context-actions">
          <button v-if="selectedIncident.status === 'open'" @click="ackIncident">认领</button>
          <button v-if="!['resolved', 'closed'].includes(selectedIncident.status)" class="danger" @click="closeIncident">解决</button>
          <button @click="runDiagnosis">重跑诊断</button>
        </div>
      </section>
    </aside>
  </div>
</template>

<style scoped src="../styles/rag-ops-chat.css"></style>
