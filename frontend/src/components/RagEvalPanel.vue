<script setup lang="ts">
/**
 * 离线测评报告浏览：左侧选报告，右侧看摘要与逐条明细。
 *
 * 检索测评（kind=retrieval）展示每题 Recall@10 / MRR / 耗时与 Top3 命中章节，
 * 命中率不足 100% 的行高亮，便于一眼看出漏检；
 * 端到端评分（kind=e2e）展示裸答与 RAG 判分对比，答案正文按需展开。
 */
import { onMounted, ref } from 'vue'
import {
  getRagEvaluation,
  listRagEvaluations,
  type RagEvalDetail,
  type RagEvalItem,
  type RagEvalReport,
} from '../api/ragAdmin'

const reports = ref<RagEvalReport[]>([])
const selected = ref<RagEvalDetail | null>(null)
const loading = ref(true)
const detailLoading = ref(false)
const error = ref('')
const expandedId = ref<string | null>(null)

const KIND_LABEL: Record<string, string> = {
  retrieval: '检索测评',
  e2e: '端到端评分',
  raw: '其他',
}

function kindLabel(kind: string): string {
  return KIND_LABEL[kind] ?? kind
}

function fmtPercent(value?: number | null): string {
  return value == null ? '-' : `${(value * 100).toFixed(2)}%`
}

function fmtMs(value?: number | null): string {
  return value == null ? '-' : `${value}ms`
}

function fmtTime(value: string): string {
  return value.replace('T', ' ').slice(0, 19)
}

/** 检索未全中（Recall@10 < 1）的行需要被看见。 */
function isMiss(item: RagEvalItem): boolean {
  return item.recall_at_10 != null && item.recall_at_10 < 1
}

/** 判分色：命中/正确为绿，答错为红，其余为警示色。 */
function verdictClass(value?: string | null): string {
  if (!value) return ''
  if (['core', 'correct'].includes(value)) return 'good'
  if (['wrong', 'miss'].includes(value)) return 'bad'
  return 'warn'
}

async function openReport(report: string): Promise<void> {
  detailLoading.value = true
  error.value = ''
  expandedId.value = null
  try {
    selected.value = await getRagEvaluation(report)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '报告读取失败'
  } finally {
    detailLoading.value = false
  }
}

onMounted(async () => {
  try {
    const { items } = await listRagEvaluations()
    reports.value = items
    if (items.length) await openReport(items[0].report)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <section class="eval-panel">
    <aside class="report-list">
      <div class="panel-head"><h2>测评报告</h2><span>{{ reports.length }} 份</span></div>
      <p v-if="loading" class="hint">正在加载...</p>
      <p v-else-if="!reports.length" class="hint">
        暂无报告：把测评 JSON 放进 app/rag_eval/reports/ 并重建镜像即可。
      </p>
      <button
        v-for="item in reports"
        :key="item.report"
        type="button"
        class="report-item"
        :class="{ active: selected?.report === item.report }"
        @click="openReport(item.report)"
      >
        <strong>{{ item.report }}</strong>
        <span>{{ kindLabel(item.kind) }} · {{ item.summary.evaluated }} 条</span>
        <span>{{ fmtTime(item.modified_at) }}</span>
      </button>
    </aside>

    <div class="report-body">
      <p v-if="error" class="hint danger">{{ error }}</p>
      <p v-else-if="detailLoading" class="hint">正在读取明细...</p>

      <template v-else-if="selected">
        <section class="panel summary-panel">
          <div class="panel-head">
            <h2>{{ selected.report }}</h2>
            <span>{{ kindLabel(selected.kind) }} · {{ selected.summary.evaluated }} 条</span>
          </div>

          <div v-if="selected.kind === 'retrieval'" class="metric-grid">
            <article><span>Recall@5</span><strong>{{ fmtPercent(selected.summary.recall_at_5) }}</strong></article>
            <article><span>Recall@10</span><strong>{{ fmtPercent(selected.summary.recall_at_10) }}</strong></article>
            <article><span>MRR</span><strong>{{ selected.summary.mrr?.toFixed(4) ?? '-' }}</strong></article>
            <article><span>平均延迟</span><strong>{{ fmtMs(selected.summary.latency_ms?.avg) }}</strong></article>
            <article><span>P95 延迟</span><strong>{{ fmtMs(selected.summary.latency_ms?.p95) }}</strong></article>
            <article>
              <span>明细 / 映射直命中</span>
              <strong>{{ selected.summary.detail_count ?? selected.summary.evaluated }} / {{ selected.summary.mapping_hit_count ?? 0 }}</strong>
            </article>
            <article><span>错误 / 排除</span><strong>{{ selected.summary.errors }} / {{ selected.summary.excluded_count ?? 0 }}</strong></article>
          </div>

          <div v-else class="verdict-row">
            <span
              v-for="(count, key) in (selected.summary.verdicts || {})"
              :key="key"
              class="verdict"
              :class="verdictClass(String(key))"
            >{{ key }} {{ count }}</span>
            <em v-if="selected.summary.net_gain">{{ selected.summary.net_gain }}</em>
          </div>
        </section>

        <section v-if="selected.kind === 'retrieval'" class="panel table-panel">
          <table>
            <thead>
              <tr><th>题号</th><th>类型</th><th>Recall@10</th><th>MRR</th><th>耗时</th><th>Top3 命中章节</th></tr>
            </thead>
            <tbody>
              <tr v-for="item in selected.items" :key="item.id" :class="{ miss: isMiss(item) }">
                <td>{{ item.id }}</td>
                <td>
                  <span v-if="item.category === 'mapping_hit'" class="verdict warn">映射表直命中</span>
                  <span v-else>{{ item.category || '-' }}</span>
                </td>
                <td>{{ fmtPercent(item.recall_at_10) }}</td>
                <td>{{ item.mrr?.toFixed(3) ?? '-' }}</td>
                <td>{{ fmtMs(item.latency_ms) }}</td>
                <td class="heading">{{ (item.top3_heading || []).join(' ｜ ') || '-' }}</td>
              </tr>
            </tbody>
          </table>
        </section>

        <section v-else class="panel table-panel">
          <table>
            <thead>
              <tr><th>题号</th><th>分组</th><th>裸答</th><th>RAG</th><th>条文数</th><th>答案</th></tr>
            </thead>
            <tbody>
              <template v-for="item in selected.items" :key="item.id">
                <tr>
                  <td>{{ item.id }}</td>
                  <td>{{ item.group || '-' }}</td>
                  <td><span class="verdict" :class="verdictClass(item.naive_verdict)">{{ item.naive_verdict || '-' }}</span></td>
                  <td><span class="verdict" :class="verdictClass(item.rag_verdict)">{{ item.rag_verdict || '-' }}</span></td>
                  <td>{{ item.chunks_used ?? '-' }}</td>
                  <td>
                    <button
                      type="button"
                      class="text-button"
                      @click="expandedId = expandedId === item.id ? null : item.id"
                    >{{ expandedId === item.id ? '收起' : `展开 ${item.answer_len ?? 0} 字` }}</button>
                  </td>
                </tr>
                <tr v-if="expandedId === item.id" class="answer-row">
                  <td colspan="6"><pre class="answer">{{ item.answer }}</pre></td>
                </tr>
              </template>
            </tbody>
          </table>
        </section>
      </template>
    </div>
  </section>
</template>

<style scoped>
.eval-panel {
  display: grid;
  grid-template-columns: minmax(240px, 320px) minmax(0, 1fr);
  gap: 14px;
  align-items: start;
}
.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
  color: var(--text-muted, #6b7280);
}
h2 { margin: 0; font-size: 14px; }
.report-list,
.panel {
  padding: 14px;
  border: 1px solid var(--border-color, #d5dae3);
  border-radius: 8px;
  background: var(--bg-card, #fff);
}
.report-item {
  display: grid;
  gap: 3px;
  width: 100%;
  margin-bottom: 6px;
  padding: 9px 10px;
  border: 1px solid var(--border-color, #e5e7eb);
  border-radius: 6px;
  background: transparent;
  text-align: left;
}
.report-item strong { font-size: 13px; overflow-wrap: anywhere; }
.report-item span { color: var(--text-muted, #6b7280); font-size: 11px; }
.report-item.active { border-color: #2563eb; background: rgba(37, 99, 235, 0.06); }
.report-body { display: grid; gap: 14px; }
.summary-panel { margin-bottom: 0; }
.metric-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 8px;
}
.metric-grid article {
  padding: 9px;
  border: 1px solid var(--border-color, #e5e7eb);
  border-radius: 6px;
}
.metric-grid span { display: block; color: var(--text-muted, #6b7280); font-size: 12px; }
.metric-grid strong { display: block; margin-top: 4px; font-size: 15px; }
.verdict-row { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; font-size: 12px; }
.verdict-row em { color: var(--text-muted, #6b7280); font-style: normal; }
.verdict { display: inline-block; padding: 2px 8px; border-radius: 4px; background: #eef2ff; font-size: 12px; }
.verdict.good { color: #067647; background: #dcfae6; }
.verdict.bad { color: #b42318; background: #fee4e2; }
.verdict.warn { color: #b45309; background: #fef0c7; }
.table-panel { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 7px 6px; border-bottom: 1px solid var(--border-color, #e5e7eb); text-align: left; }
th { color: var(--text-muted, #6b7280); font-weight: 500; white-space: nowrap; }
tbody tr.miss { background: #fffbfa; }
tbody tr.miss td:nth-child(3) { color: #b42318; font-weight: 600; }
.heading { color: var(--text-muted, #4b5563); font-size: 12px; }
.answer-row td { padding: 0 6px 10px; }
.answer {
  max-height: 320px;
  margin: 0;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-family: inherit;
  font-size: 12px;
  line-height: 1.65;
}
.text-button {
  min-height: auto;
  padding: 0;
  border: 0;
  background: transparent;
  color: #1d4ed8;
  cursor: pointer;
}
.hint { color: var(--text-muted, #6b7280); font-size: 13px; }
.hint.danger { color: #b42318; }
@media (max-width: 900px) {
  .eval-panel { grid-template-columns: 1fr; }
}
</style>
