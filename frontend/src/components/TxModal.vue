<script setup lang="ts">
/** 交易流水弹窗（旧版 .payment-overlay：类型过滤 + 分页） */
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { getTransactions, type Transaction } from '../api/payment'

const { t } = useI18n()
const emit = defineEmits<{ close: [] }>()
const items = ref<Transaction[]>([])
const loading = ref(false)
const loadError = ref('')
const page = ref(1)
const totalPages = ref(1)
const filter = ref('')

const FILTERS = [
  { key: '', label: 'tx_all' },
  { key: 'recharge', label: 'tx_recharge' },
  { key: 'consume', label: 'tx_consume' },
]

async function load(p: number, f: string) {
  loading.value = true
  loadError.value = ''
  try {
    const resp = await getTransactions(p, 10, f || undefined)
    items.value = resp.items
    page.value = resp.page
    totalPages.value = resp.total_pages
  } catch {
    // 加载失败给出可见提示，而不是静默空白 + 未处理拒绝
    loadError.value = t('tx_load_failed')
  } finally {
    loading.value = false
  }
}

function setFilter(f: string) {
  filter.value = f
  load(1, f)
}

/** 金额符号按类型定：DB 里 consume 存正数，展示层负责 - 号（充值 +/退款 +） */
function fmtAmount(tx: Transaction): string {
  const sign = tx.tx_type === 'consume' ? '-' : '+'
  return `${sign}${Math.abs(tx.amount).toFixed(2)}`
}

/** ISO 时间转本地可读；非法值原样兜底 */
function fmtTime(iso: string): string {
  const d = new Date(iso)
  return isNaN(+d) ? iso : d.toLocaleString('zh-CN', { hour12: false })
}


onMounted(() => load(1, filter.value))
</script>

<template>
  <div class="payment-overlay show" @click.self="emit('close')">
    <div class="payment-modal">
      <div class="modal-header">
        <h3>{{ t('transactions') }}</h3>
        <button class="close-btn" @click="emit('close')">✕</button>
      </div>
      <div class="modal-body">
        <div style="display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap;">
          <button
            v-for="f in FILTERS"
            :key="f.key"
            class="tx-filter-btn"
            :class="{ active: filter === f.key }"
            :style="filter === f.key ? 'background:var(--primary);color:#fff;' : ''"
            @click="setFilter(f.key)"
          >
            {{ t(f.label) }}
          </button>
        </div>
        <div v-if="loading" style="text-align:center;padding:30px;color:#999;">{{ t('loading') }}</div>
        <div v-else-if="loadError" style="text-align:center;padding:30px;color:#ef4444;">{{ loadError }}</div>
        <div v-else-if="!items.length" style="text-align:center;padding:30px;color:#999;">{{ t('tx_empty') }}</div>
        <div v-else class="tx-list">
          <div v-for="tx in items" :key="tx.id" class="tx-item">
            <span class="tx-icon">{{ tx.tx_type === 'recharge' ? '💰' : tx.tx_type === 'consume' ? '💬' : '↩️' }}</span>
            <span class="tx-info">
              <span class="tx-type">{{ t('tx_' + tx.tx_type) || tx.tx_type }}</span>
              <span class="tx-time">{{ fmtTime(tx.created_at) }}</span>
            </span>
            <span class="tx-amount" :class="tx.tx_type === 'consume' ? 'consume' : 'recharge'">
              {{ fmtAmount(tx) }}
            </span>
          </div>
        </div>
        <div v-if="totalPages > 1" style="display:flex;justify-content:center;gap:12px;margin-top:12px;">
          <button :disabled="page <= 1" @click="load(page - 1, filter)">{{ t('prev_page') }}</button>
          <span style="font-size:12px;color:#666;align-self:center;">{{ page }} / {{ totalPages }}</span>
          <button :disabled="page >= totalPages" @click="load(page + 1, filter)">{{ t('next_page') }}</button>
        </div>
      </div>
    </div>
  </div>
</template>
