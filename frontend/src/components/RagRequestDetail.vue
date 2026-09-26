<script setup lang="ts">
/** 请求详情：展示阶段时间线、召回证据和引用校验摘要。 */
import type { RagRequest, RagRequestContent, RagSpan } from '../api/ragAdmin'
import { label_retrieval_method, label_route, label_stage, label_status } from '../utils/ragLabels'

type RagRequestDetail = RagRequest & {
  spans: RagSpan[]
  content: RagRequestContent
}

defineProps<{
  request: RagRequestDetail
}>()

defineEmits<{
  close: []
}>()

/** 格式化后端 ISO 时间，保留秒级精度。 */
function fmtTime(value?: string | null): string {
  return value ? value.slice(0, 19).replace('T', ' ') : '-'
}

/** 格式化毫秒耗时，空值统一显示短横线。 */
function fmtMs(value?: number | null): string {
  return value == null ? '-' : `${value}ms`
}

/** 格式化相似度等可为空的小数，避免模板中重复判断。 */
function fmtNumber(value?: number | null, digits = 4): string {
  return value == null ? '-' : value.toFixed(digits)
}

/** 将阶段属性序列化，便于排查未单独建模的字段。 */
function fmtJson(value?: Record<string, unknown>): string {
  if (!value || !Object.keys(value).length) return '-'
  return JSON.stringify(value, null, 2)
}

/** 判断阶段是否存在可展示属性，控制折叠项显示。 */
function hasAttributes(value?: Record<string, unknown>): boolean {
  return Boolean(value && Object.keys(value).length)
}

/** 格式化无依据引用，明确区分“无异常”和“未记录”。 */
function citationText(values: string[] | undefined): string {
  if (!values) return '未记录'
  return values.length ? values.join('、') : '无'
}

/** 格式化用户反馈，空值不伪装成零分。 */
function feedbackText(value: number | null | undefined): string {
  return value == null ? '未记录' : String(value)
}
</script>

<template>
  <aside class="detail-panel request-detail">
    <div class="panel-head">
      <h2>请求详情</h2>
      <button class="text-button" @click="$emit('close')">关闭</button>
    </div>
    <dl class="request-meta">
      <dt>请求标识</dt>
      <dd class="mono">{{ request.request_uid }}</dd>
      <dt>状态 / 错误码</dt>
      <dd>{{ label_status(request.status) }} / {{ request.error_code || '-' }}</dd>
      <dt>路径 / 模型</dt>
      <dd>{{ label_route(request.route) }} / {{ request.selected_model || '-' }}</dd>
      <dt>时间</dt>
      <dd>{{ fmtTime(request.created_at) }}</dd>
      <dt>令牌用量</dt>
      <dd>{{ request.input_tokens ?? '-' }} 输入 / {{ request.output_tokens ?? '-' }} 输出</dd>
    </dl>

    <section class="evidence-summary">
      <h3>关键证据</h3>
      <dl>
        <dt>依据校验</dt>
        <dd>{{ citationText(request.content.ungrounded_citations) }}</dd>
        <dt>命中条文</dt>
        <dd>{{ request.content.cited_numbers.length ? request.content.cited_numbers.join('、') : '-' }}</dd>
        <dt>用户反馈</dt>
        <dd>{{ feedbackText(request.content.feedback) }}</dd>
      </dl>
      <div v-if="request.content.retrieval_runs.length" class="retrieval-runs">
        <article v-for="run in request.content.retrieval_runs" :key="run.created_at + run.method">
          <div class="run-head">
            <strong>{{ label_retrieval_method(run.method) }}</strong>
            <span>{{ fmtMs(run.latency_ms) }}</span>
          </div>
          <p>
            trgm {{ run.trgm_hits ?? '-' }} / vector {{ run.vec_hits ?? '-' }}
            · 返回 {{ run.returned_count ?? '-' }}
            · rerank {{ run.rerank_used === true ? '已执行' : run.rerank_used === false ? '未执行' : '-' }}
          </p>
          <p>
            最大相似度 trgm {{ fmtNumber(run.max_trgm_sim) }} / vector {{ fmtNumber(run.max_vec_sim) }}
          </p>
        </article>
      </div>
      <p v-else class="empty">暂无召回运行记录</p>
    </section>

    <section class="stage-section">
      <h3>执行阶段</h3>
      <ol class="timeline">
        <li v-for="span in request.spans" :key="span.span_uid" class="timeline-item">
          <div class="stage-row">
            <span class="stage">
              {{ label_stage(span.stage) }}<small v-if="span.round_no > 1"> #{{ span.round_no }}</small>
            </span>
            <span :class="['span-status', span.status]">{{ label_status(span.status) }}</span>
            <span>{{ fmtMs(span.latency_ms) }}</span>
            <span>{{ span.result_count == null ? '' : `结果数 ${span.result_count}` }}</span>
          </div>
          <p v-if="span.error_code" class="stage-error">错误码 {{ span.error_code }}</p>
          <details v-if="hasAttributes(span.attributes)" class="stage-attributes">
            <summary>阶段属性</summary>
            <pre>{{ fmtJson(span.attributes) }}</pre>
          </details>
        </li>
        <li v-if="!request.spans.length" class="empty">该请求没有阶段记录</li>
      </ol>
    </section>
  </aside>
</template>

<style scoped>
.request-detail { min-width: 0; }
.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
h2, h3 { margin: 0; }
h2 { font-size: 14px; }
h3 { margin-bottom: 8px; font-size: 13px; }
.request-meta,
.evidence-summary dl {
  display: grid;
  grid-template-columns: 110px 1fr;
  gap: 7px 10px;
  margin: 0;
  font-size: 13px;
}
.request-meta { margin-top: 12px; }
dt { color: var(--text-muted, #6b7280); }
dd { margin: 0; overflow-wrap: anywhere; }
.mono, pre { font-family: Consolas, monospace; }
.evidence-summary,
.stage-section {
  margin-top: 16px;
  padding-top: 14px;
  border-top: 1px solid var(--border-color, #e5e7eb);
}
.retrieval-runs {
  display: grid;
  gap: 8px;
  margin-top: 10px;
}
.retrieval-runs article {
  padding: 9px;
  border: 1px solid var(--border-color, #e5e7eb);
  border-radius: 6px;
  background: var(--bg-body, #f8fafc);
  font-size: 12px;
}
.run-head { display: flex; justify-content: space-between; gap: 8px; }
.retrieval-runs p { margin: 5px 0 0; color: var(--text-secondary, #475467); }
.timeline { display: grid; gap: 8px; margin: 0; padding: 0; list-style: none; }
.timeline-item {
  padding: 9px;
  border: 1px solid var(--border-color, #e5e7eb);
  border-radius: 6px;
}
.stage-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto auto;
  gap: 8px;
  align-items: center;
  font-size: 12px;
}
.stage small { margin-left: 4px; color: var(--text-muted, #6b7280); }
.span-status.failed { color: #b42318; }
.stage-error { margin: 6px 0 0; color: #b42318; font-size: 12px; }
.stage-attributes { margin-top: 7px; }
.stage-attributes summary { color: #1d4ed8; cursor: pointer; font-size: 12px; }
pre {
  margin: 7px 0 0;
  padding: 8px;
  overflow: auto;
  border-radius: 5px;
  background: var(--bg-body, #f5f7fa);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 11px;
}
.empty { color: var(--text-muted, #6b7280); font-size: 12px; }
.text-button {
  min-height: auto;
  padding: 0;
  border: 0;
  background: transparent;
  color: #1d4ed8;
  cursor: pointer;
}
@media (max-width: 900px) {
  .stage-row { grid-template-columns: 1fr auto; }
}
</style>
