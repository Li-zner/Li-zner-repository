<script setup lang="ts">
/** 事件详情：样本证据、推荐修复和人工动作入口。 */
import { computed, ref, watch } from 'vue'
import {
  acknowledgeIncident,
  cancelRagAction,
  getRagIncidentRecommendations,
  planRagAction,
  resolveIncident,
  type RagAction,
  type RagIncident,
  type RagIncidentRecommendation,
} from '../api/ragAdmin'
import { label_action, label_risk, label_status } from '../utils/ragLabels'

const props = defineProps<{
  incident: RagIncident
}>()

const emit = defineEmits<{
  close: []
  openRequest: [requestUid: string]
  refresh: []
}>()

const recommendations = ref<RagIncidentRecommendation[]>([])
const recommendationLoading = ref(false)
const recommendationError = ref('')
const recommendationBusy = ref('')
const submittedAction = ref<RagAction | null>(null)
const actionError = ref('')
const actionKey = ref('rerun_diagnosis')
const actionQuery = ref('')
const actionCacheCtx = ref('')
const actionRerankTop = ref(8)
const actionReason = ref('')
const mutationBusy = ref(false)

const actionOptions = [
  { value: 'rerun_diagnosis', label: '立即重跑诊断' },
  { value: 'probe_health', label: '健康探测' },
  { value: 'clear_exact_cache', label: '精确清理缓存' },
  { value: 'adjust_rerank_top', label: '调整重排候选池' },
]

const incidentSamples = computed(() => (props.incident.verdicts || []).slice(0, 5))

/** 加载当前事件的确定性推荐，切换事件时重置已提交动作。 */
async function loadRecommendations(): Promise<void> {
  recommendationLoading.value = true
  recommendationError.value = ''
  submittedAction.value = null
  try {
    recommendations.value = (
      await getRagIncidentRecommendations(props.incident.incident_id)
    ).items
  } catch (err) {
    recommendations.value = []
    recommendationError.value = err instanceof Error ? err.message : '推荐修复加载失败'
  } finally {
    recommendationLoading.value = false
  }
}

/** 事件编号或规则变化时重新读取推荐，避免展示旧事件建议。 */
watch(
  [() => props.incident.incident_id, () => props.incident.code],
  () => void loadRecommendations(),
  { immediate: true },
)

/** 将后端模式转成运营可读文案。 */
function modeLabel(mode: RagIncidentRecommendation['mode']): string {
  if (mode === 'auto') return '自动执行'
  if (mode === 'approval') return '需要审批'
  return '人工处理'
}

/** 按模式生成与后端语义一致的按钮文案。 */
function submitLabel(mode: RagIncidentRecommendation['mode']): string {
  return mode === 'auto' ? '生成并执行' : '提交审批'
}

/** 只有尚未执行的动作才允许撤销。 */
function canCancelAction(action: RagAction | null): boolean {
  return Boolean(
    action && ['proposed', 'waiting_approval', 'queued'].includes(action.status),
  )
}

/** 提交推荐动作，成功后保留动作用于立即撤销。 */
async function submitRecommendation(
  item: RagIncidentRecommendation,
): Promise<void> {
  if (!item.action_key || item.mode === 'manual') return
  recommendationBusy.value = `${item.action_key}:${JSON.stringify(item.params)}`
  recommendationError.value = ''
  try {
    submittedAction.value = await planRagAction(
      props.incident.incident_id,
      item.action_key,
      item.params,
      item.reason,
    )
    emit('refresh')
  } catch (err) {
    recommendationError.value = err instanceof Error ? err.message : '动作提交失败'
  } finally {
    recommendationBusy.value = ''
  }
}

/** 撤销刚提交的推荐动作，动作状态以服务端返回值为准。 */
async function cancelSubmittedAction(): Promise<void> {
  const action = submittedAction.value
  if (!canCancelAction(action)) return
  recommendationBusy.value = `cancel:${action!.action_id}`
  recommendationError.value = ''
  try {
    submittedAction.value = await cancelRagAction(action!.action_id)
    emit('refresh')
  } catch (err) {
    recommendationError.value = err instanceof Error ? err.message : '撤销动作失败'
  } finally {
    recommendationBusy.value = ''
  }
}

/** 标记事件已认领并通知父级刷新列表。 */
async function acknowledge(): Promise<void> {
  await withMutation(async () => {
    await acknowledgeIncident(props.incident.incident_id)
    emit('refresh')
  })
}

/** 标记事件已解决并通知父级刷新列表。 */
async function resolve(): Promise<void> {
  await withMutation(async () => {
    await resolveIncident(props.incident.incident_id)
    emit('refresh')
  })
}

/** 创建人工指定的修复动作，保留原有高级操作能力。 */
async function submitManualAction(): Promise<void> {
  actionError.value = ''
  await withMutation(async () => {
    try {
      const params: Record<string, unknown> = {}
      if (actionKey.value === 'clear_exact_cache') {
        params.query = actionQuery.value.trim()
        params.cache_ctx = actionCacheCtx.value.trim()
      }
      if (actionKey.value === 'adjust_rerank_top') {
        params.value = Number(actionRerankTop.value)
      }
      await planRagAction(
        props.incident.incident_id,
        actionKey.value,
        params,
        actionReason.value.trim(),
      )
      actionReason.value = ''
      emit('refresh')
    } catch (err) {
      actionError.value = err instanceof Error ? err.message : '动作创建失败'
    }
  })
}

/** 写操作串行化，避免双击造成重复动作。 */
async function withMutation(task: () => Promise<void>): Promise<void> {
  if (mutationBusy.value) return
  mutationBusy.value = true
  try {
    await task()
  } finally {
    mutationBusy.value = false
  }
}

/** 格式化时间，空值显示短横线。 */
function fmtTime(value?: string | null): string {
  return value ? value.slice(0, 19).replace('T', ' ') : '-'
}

/** 格式化推荐参数或样本 patch。 */
function fmtJson(value?: Record<string, unknown>): string {
  if (!value || !Object.keys(value).length) return '-'
  return JSON.stringify(value)
}
</script>

<template>
  <aside class="detail-panel incident-detail">
    <div class="panel-head">
      <h2>事件 #{{ incident.incident_id }}</h2>
      <button class="text-button" @click="emit('close')">关闭</button>
    </div>
    <p class="incident-title">{{ incident.title }}</p>
    <dl>
      <dt>疑似原因</dt>
      <dd>{{ incident.suspected_cause || '-' }}</dd>
      <dt>建议动作</dt>
      <dd>{{ incident.recommended_action || '-' }}</dd>
      <dt>首末出现</dt>
      <dd>{{ fmtTime(incident.first_seen_at) }} / {{ fmtTime(incident.last_seen_at) }}</dd>
    </dl>

    <section class="recommendation-section">
      <h3>推荐修复</h3>
      <p v-if="recommendationLoading" class="empty">正在加载推荐...</p>
      <p v-else-if="recommendationError" class="error">{{ recommendationError }}</p>
      <div v-else-if="recommendations.length" class="recommendation-list">
        <article
          v-for="(item, index) in recommendations"
          :key="`${item.action_key}-${item.mode}-${index}`"
          class="recommendation-card"
        >
          <div class="recommendation-head">
            <strong>{{ item.action_key ? label_action(item.action_key) : '人工处理建议' }}</strong>
            <span class="recommendation-mode">{{ modeLabel(item.mode) }}</span>
          </div>
          <p>{{ item.reason || '-' }}</p>
          <p v-if="item.risk_level">风险等级：{{ label_risk(item.risk_level) }}</p>
          <pre v-if="Object.keys(item.params || {}).length">{{ fmtJson(item.params) }}</pre>
          <div v-if="item.mode !== 'manual' && item.action_key" class="recommendation-actions">
            <button
              :disabled="Boolean(recommendationBusy)"
              @click="submitRecommendation(item)"
            >
              {{ submitLabel(item.mode) }}
            </button>
            <button
              v-if="
                submittedAction?.action_key === item.action_key
                && canCancelAction(submittedAction)
              "
              class="danger"
              :disabled="Boolean(recommendationBusy)"
              @click="cancelSubmittedAction"
            >
              撤销刚才提交
            </button>
            <span
              v-if="submittedAction?.action_key === item.action_key"
              class="submitted-status"
            >
              动作 #{{ submittedAction.action_id }}：{{ label_status(submittedAction.status) }}
            </span>
          </div>
          <p v-else class="manual-hint">仅提供建议，不创建动作。</p>
        </article>
      </div>
      <p v-else class="empty">暂无推荐修复。</p>
    </section>

    <section class="verdict-samples">
      <h3>样本证据</h3>
      <div v-for="sample in incidentSamples" :key="sample.id" class="verdict-sample">
        <div class="sample-head">
          <span class="severity" :class="sample.severity">{{ sample.code }}</span>
          <span class="sample-query">{{ sample.query_snippet || '-' }}</span>
          <button
            v-if="sample.request_uid"
            class="text-button"
            @click="emit('openRequest', sample.request_uid)"
          >
            查看请求
          </button>
        </div>
        <ul class="evidence-list">
          <li v-for="(ev, index) in (sample.evidence || []).slice(0, 5)" :key="index">{{ ev }}</li>
        </ul>
        <p v-if="sample.action" class="sample-action">{{ sample.action }}</p>
        <p v-if="fmtJson(sample.patch) !== '-'" class="sample-patch mono">
          {{ fmtJson(sample.patch) }}
        </p>
      </div>
      <p v-if="!incidentSamples.length" class="empty">暂无样本结论</p>
    </section>

    <div class="button-row">
      <button v-if="incident.status === 'open'" @click="acknowledge">认领</button>
      <button
        v-if="!['resolved', 'closed'].includes(incident.status)"
        class="danger"
        @click="resolve"
      >
        解决
      </button>
    </div>

    <section class="action-plan">
      <h3>人工生成动作</h3>
      <select v-model="actionKey">
        <option v-for="item in actionOptions" :key="item.value" :value="item.value">
          {{ item.label }}
        </option>
      </select>
      <template v-if="actionKey === 'clear_exact_cache'">
        <input v-model="actionQuery" placeholder="需要失效的原始问题">
        <input v-model="actionCacheCtx" placeholder="缓存上下文">
      </template>
      <template v-if="actionKey === 'adjust_rerank_top'">
        <input
          v-model.number="actionRerankTop"
          type="number"
          min="5"
          max="20"
          placeholder="候选池 5~20"
        >
      </template>
      <input v-model="actionReason" placeholder="原因">
      <button :disabled="mutationBusy" @click="submitManualAction">创建动作</button>
      <p v-if="actionError" class="error">{{ actionError }}</p>
    </section>
  </aside>
</template>

<style scoped>
.incident-detail { min-width: 0; }
.panel-head,
.button-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
h2, h3 { margin: 0; font-size: 14px; }
.incident-title { font-weight: 600; }
dl {
  display: grid;
  grid-template-columns: 110px 1fr;
  gap: 7px 10px;
  margin: 0;
  font-size: 13px;
}
dt { color: var(--text-muted, #6b7280); }
dd { margin: 0; overflow-wrap: anywhere; }
.recommendation-section,
.verdict-samples,
.action-plan {
  margin-top: 16px;
  padding-top: 14px;
  border-top: 1px solid var(--border-color, #e5e7eb);
}
.recommendation-list,
.verdict-samples { display: grid; gap: 10px; }
.recommendation-card,
.verdict-sample {
  display: grid;
  gap: 6px;
  padding: 10px;
  border: 1px solid var(--border-color, #e5e7eb);
  border-radius: 8px;
}
.recommendation-head,
.sample-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.recommendation-head { justify-content: space-between; }
.recommendation-card p { margin: 0; font-size: 12px; }
.recommendation-mode {
  padding: 1px 7px;
  border-radius: 4px;
  background: #eef2ff;
  color: #3538cd;
  font-size: 12px;
}
.recommendation-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.submitted-status,
.manual-hint {
  color: var(--text-muted, #6b7280);
  font-size: 12px;
}
.sample-query {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
}
.evidence-list {
  display: grid;
  gap: 2px;
  margin: 0;
  padding-left: 18px;
  color: var(--text-secondary, #475467);
  font-size: 12px;
}
.sample-action,
.sample-patch { margin: 0; font-size: 12px; }
.sample-patch { color: var(--text-muted, #6b7280); word-break: break-all; }
.button-row { justify-content: flex-start; margin-top: 14px; }
.action-plan { display: grid; gap: 8px; }
.mono, pre { font-family: Consolas, monospace; }
pre {
  margin: 0;
  padding: 8px;
  overflow: auto;
  border-radius: 5px;
  background: var(--bg-body, #f5f7fa);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 11px;
}
.empty { color: var(--text-muted, #6b7280); font-size: 12px; }
.error { color: #b42318; font-size: 12px; }
.text-button {
  min-height: auto;
  padding: 0;
  border: 0;
  background: transparent;
  color: #1d4ed8;
  cursor: pointer;
}
</style>
