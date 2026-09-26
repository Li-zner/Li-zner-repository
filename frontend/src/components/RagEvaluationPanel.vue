<script setup lang="ts">
/** 最新一份检索测评指标（离线基线或 chat 真跑反向复算，后端按 mtime 取最新）。 */
import { computed } from 'vue'
import type { RagStatus } from '../api/ragAdmin'
import { fmtDuration } from '../utils/ragFormat'

const props = defineProps<{
  evaluation: NonNullable<RagStatus['evaluation']>
  runtime: RagStatus['runtime_config']
}>()

// live-* 前缀 = chat 链路真跑后由 score_live_eval.py 从监控 trace 复算的报告
const isLive = computed(() => props.evaluation.report?.startsWith('live-'))

function formatPercent(value: number | null): string {
  return value == null ? '-' : `${(value * 100).toFixed(2)}%`
}

function formatNumber(value: number | null): string {
  return value == null ? '-' : value.toFixed(4)
}
</script>

<template>
  <section class="evaluation-panel">
    <div class="panel-head">
      <div>
        <h2>检索评测 · {{ isLive ? 'chat 真跑（监控口径）' : '离线固定基线' }}</h2>
        <p>{{ evaluation.split }} / {{ evaluation.evaluated }} 条 / {{ evaluation.report }}（逐条明细见「测评」分区）</p>
      </div>
      <div class="panel-meta">
        <span :class="{ override: runtime.rerank_top_overridden }">
          重排候选池 {{ runtime.rerank_top }}
          {{ runtime.rerank_top_observed != null ? '（网关实测）' : '（近期无 trace，回落配置值）' }}
          {{ runtime.rerank_top_overridden ? ' · Redis 覆盖中' : '' }}
        </span>
        <span v-if="evaluation.errors" class="error">{{ evaluation.errors }} 条错误</span>
      </div>
    </div>
    <div class="evaluation-grid">
      <article><span>Recall@5</span><strong>{{ formatPercent(evaluation.recall_at_5) }}</strong></article>
      <article><span>Precision@5</span><strong>{{ formatPercent(evaluation.precision_at_5) }}</strong></article>
      <article><span>Recall@10</span><strong>{{ formatPercent(evaluation.recall_at_10) }}</strong></article>
      <article><span>Precision@10</span><strong>{{ formatPercent(evaluation.precision_at_10) }}</strong></article>
      <article><span>MRR</span><strong>{{ formatNumber(evaluation.mrr) }}</strong></article>
      <article><span>平均 / P95 延迟</span><strong>{{ fmtDuration(evaluation.latency_ms.avg ?? null) }} / {{ fmtDuration(evaluation.latency_ms.p95 ?? null) }}</strong></article>
    </div>
    <div class="zero-hit">
      <span>Recall@10 零命中</span>
      <template v-if="evaluation.zero_hit_at_10.length">
        <code v-for="item in evaluation.zero_hit_at_10" :key="item">{{ item }}</code>
      </template>
      <em v-else>无</em>
    </div>
  </section>
</template>

<style scoped>
.evaluation-panel {
  margin-bottom: 14px;
  padding: 14px;
  border: 1px solid var(--border-color, #d5dae3);
  border-radius: 8px;
  background: var(--bg-card, #fff);
}
.panel-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.panel-meta { display: grid; justify-items: end; gap: 4px; font-size: 12px; }
.panel-meta .override { color: #b45309; font-weight: 600; }
h2 { margin: 0; font-size: 14px; }
p { margin: 3px 0 0; color: var(--text-muted, #6b7280); font-size: 12px; }
.evaluation-grid {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 8px;
  margin-top: 12px;
}
article {
  padding: 9px;
  border: 1px solid var(--border-color, #e5e7eb);
  border-radius: 6px;
}
article span { display: block; color: var(--text-muted, #6b7280); font-size: 12px; }
article strong { display: block; margin-top: 4px; font-size: 15px; }
.zero-hit {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 10px;
  font-size: 12px;
}
.zero-hit > span { color: var(--text-muted, #6b7280); }
code {
  padding: 2px 6px;
  border-radius: 4px;
  background: #fef0c7;
  color: #b45309;
}
.error { color: #b42318; font-size: 12px; }
@media (max-width: 900px) {
  .evaluation-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
</style>
