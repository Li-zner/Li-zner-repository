<script setup lang="ts">
/**
 * 只读观测「问数」（P1）：用大白话提问，后端把问题翻成 SQL，
 * 交给只读角色执行，再把真实数据行回给模型写结论。
 *
 * 界面按"面向不看代码的运营"来排：问题、结论、数据表在前，
 * 模型产出的 SQL 与耗时收进折叠的「技术明细」——它是排障线索，不是阅读材料。
 * 只读是这个分区的前提：白名单表 + 只读角色，任何写操作都会被守卫或数据库拒掉。
 */
import { computed, onMounted, ref } from 'vue'
import {
  askData,
  listDataTables,
  type DataAskResult,
  type DataTableInfo,
} from '../api/ragAdmin'

interface Turn extends DataAskResult {
  question: string
  showSql: boolean
}

/** 结果表默认只铺前若干行，剩下靠"展开全部"，避免几百行把面板撑爆。 */
const VISIBLE_ROWS = 20

const tables = ref<DataTableInfo[]>([])
const turns = ref<Turn[]>([])
const input = ref('')
const busy = ref(false)
const error = ref('')
const loaded = ref(false)

const EXAMPLES = [
  '最近 30 天有多少笔成功支付？',
  '支付订单里状态最多的是哪一种？',
  '知识库当前有多少条有效条文？',
  '最近一周有多少条问数请求失败？',
]

const canAsk = computed(() => input.value.trim().length > 0 && !busy.value)

/** 多轮追问要带上上一轮的表与口径，但只送问答文本，不把整张结果表塞回去。 */
function historyPayload() {
  const pairs: Array<{ role: string; content: string }> = []
  for (const turn of turns.value.slice(-6)) {
    pairs.push({ role: 'user', content: turn.question })
    pairs.push({ role: 'assistant', content: turn.answer })
  }
  return pairs
}

async function ask(): Promise<void> {
  const question = input.value.trim()
  if (!question || busy.value) return
  busy.value = true
  error.value = ''
  try {
    const result = await askData(question, historyPayload())
    turns.value.unshift({ ...result, question, showSql: false })
    input.value = ''
  } catch (err) {
    // 守卫拒绝（422）与上游失败（502）的文案后端已写成人话，直接显示即可
    error.value = err instanceof Error ? err.message : '问数失败'
  } finally {
    busy.value = false
  }
}

function visibleRows(turn: Turn): Array<Record<string, string | number | boolean | null>> {
  return turn.rows.slice(0, VISIBLE_ROWS)
}

function text(value: string | number | boolean | null): string {
  if (value === null || value === undefined) return '-'
  return typeof value === 'string' ? value : String(value)
}

onMounted(async () => {
  try {
    const { tables: rows } = await listDataTables()
    tables.value = rows
  } catch (err) {
    error.value = err instanceof Error ? err.message : '白名单表读取失败'
  } finally {
    loaded.value = true
  }
})
</script>

<template>
  <section class="ask-panel">
    <div class="ask-box panel">
      <div class="panel-head">
        <h2>问数</h2>
        <span>只读查询，不会改动任何数据</span>
      </div>
      <textarea
        v-model="input"
        class="ask-input"
        rows="2"
        placeholder="用大白话描述你想看的数据，例如：最近 30 天有多少笔成功支付？"
        @keydown.ctrl.enter.prevent="ask"
        @keydown.meta.enter.prevent="ask"
      />
      <div class="ask-actions">
        <div class="examples">
          <button
            v-for="example in EXAMPLES"
            :key="example"
            type="button"
            class="chip"
            :disabled="busy"
            @click="input = example"
          >{{ example }}</button>
        </div>
        <button type="button" class="primary" :disabled="!canAsk" @click="ask">
          {{ busy ? '正在查询...' : '提问' }}
        </button>
      </div>
      <p class="hint">Ctrl + Enter 直接提问；接着上一问继续追问会自动带上上下文。</p>
      <p v-if="error" class="hint danger">{{ error }}</p>
    </div>

    <div class="ask-body">
      <p v-if="!turns.length && !busy" class="hint empty">
        还没有提问。可以先点上面任一个问题试试。
      </p>

      <article v-for="(turn, index) in turns" :key="index" class="panel turn">
        <h3 class="question">{{ turn.question }}</h3>
        <p class="answer">{{ turn.answer }}</p>

        <p v-if="turn.row_count" class="hint">
          共 {{ turn.row_count }} 行
          <span v-if="turn.rows.length > VISIBLE_ROWS">，下方仅显示前 {{ VISIBLE_ROWS }} 行</span>
          <strong v-if="turn.truncated" class="warn">　结果已按上限截断，不代表全量</strong>
        </p>

        <div v-if="turn.row_count" class="table-wrap">
          <table>
            <thead>
              <tr><th v-for="col in turn.columns" :key="col">{{ col }}</th></tr>
            </thead>
            <tbody>
              <tr v-for="(row, rowIndex) in visibleRows(turn)" :key="rowIndex">
                <td v-for="col in turn.columns" :key="col">{{ text(row[col]) }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="tech">
          <button type="button" class="text-button" @click="turn.showSql = !turn.showSql">
            {{ turn.showSql ? '收起技术明细' : '展开技术明细（SQL 与耗时）' }}
          </button>
          <template v-if="turn.showSql">
            <pre class="sql">{{ turn.sql }}</pre>
            <p class="hint">耗时 {{ turn.elapsed_ms }}ms；只读角色执行，SQL 由模型生成并经守卫校验。</p>
          </template>
        </div>
      </article>
    </div>

    <aside class="panel table-list">
      <div class="panel-head">
        <h2>可问的数据</h2>
        <span>{{ loaded ? `${tables.length} 张表` : '正在加载...' }}</span>
      </div>
      <dl>
        <template v-for="item in tables" :key="item.name">
          <dt>{{ item.name }}</dt>
          <dd>{{ item.usage }}</dd>
        </template>
      </dl>
      <p class="hint">
        名单之外的表（含用户账号、密钥、审计日志）对这个只读账号不可见，
        提问涉及它们会直接被拒。
      </p>
    </aside>
  </section>
</template>

<style scoped>
.ask-panel {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(240px, 320px);
  gap: 14px;
  align-items: start;
}
.panel {
  padding: 14px;
  border: 1px solid var(--border-color, #d5dae3);
  border-radius: 8px;
  background: var(--bg-card, #fff);
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
h3.question { margin: 0 0 6px; font-size: 14px; }
.ask-box { grid-column: 1 / 2; }
.ask-body { grid-column: 1 / 2; display: grid; gap: 12px; }
.table-list { grid-column: 2 / 3; grid-row: 1 / 3; }
.ask-input {
  width: 100%;
  padding: 9px 10px;
  border: 1px solid var(--border-color, #d5dae3);
  border-radius: 6px;
  font: inherit;
  font-size: 13px;
  line-height: 1.6;
  resize: vertical;
}
.ask-actions {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 10px;
  margin-top: 8px;
}
.examples { display: flex; flex-wrap: wrap; gap: 6px; }
.chip {
  padding: 4px 9px;
  border: 1px solid var(--border-color, #e5e7eb);
  border-radius: 999px;
  background: transparent;
  color: var(--text-muted, #4b5563);
  font-size: 12px;
  cursor: pointer;
}
.chip:disabled { cursor: default; opacity: 0.6; }
.primary {
  min-height: 32px;
  padding: 6px 16px;
  border: 1px solid #2563eb;
  border-radius: 6px;
  background: #2563eb;
  color: #fff;
  font-size: 13px;
  cursor: pointer;
}
.primary:disabled { border-color: #c7d2fe; background: #c7d2fe; cursor: default; }
.turn { display: grid; gap: 8px; }
.answer { margin: 0; font-size: 14px; line-height: 1.7; white-space: pre-wrap; overflow-wrap: anywhere; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td {
  padding: 6px;
  border-bottom: 1px solid var(--border-color, #e5e7eb);
  text-align: left;
  overflow-wrap: anywhere;
}
th { color: var(--text-muted, #6b7280); font-weight: 500; white-space: nowrap; }
.tech { display: grid; gap: 6px; border-top: 1px dashed var(--border-color, #e5e7eb); padding-top: 8px; }
.sql {
  margin: 0;
  padding: 9px;
  border: 1px solid var(--border-color, #e5e7eb);
  border-radius: 6px;
  background: #f9fafb;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 12px;
  line-height: 1.6;
}
dl { margin: 0; }
dt { font-size: 12px; font-family: ui-monospace, Consolas, monospace; }
dd { margin: 0 0 8px; color: var(--text-muted, #6b7280); font-size: 12px; line-height: 1.6; }
.text-button {
  min-height: auto;
  padding: 0;
  border: 0;
  background: transparent;
  color: #1d4ed8;
  font-size: 12px;
  cursor: pointer;
  justify-self: start;
}
.hint { margin: 8px 0 0; color: var(--text-muted, #6b7280); font-size: 12px; line-height: 1.6; }
.hint.danger { color: #b42318; }
.hint.empty { margin-top: 0; }
.warn { color: #b45309; }
@media (max-width: 900px) {
  .ask-panel { grid-template-columns: 1fr; }
  .ask-box, .ask-body, .table-list { grid-column: 1 / 2; grid-row: auto; }
}
</style>
