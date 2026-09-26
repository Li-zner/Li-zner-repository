<script setup lang="ts">
/**
 * 动作分区：待审批队列 + 业务写动作的提议表单。
 *
 * 列表与详情沿用原来的口径（父组件负责取数），本组件只做两件事：
 * 展示动作行、给出"提议一个不挂事件的业务动作"的入口。
 * 面向非技术操作者：选项与目标都用业务中文，动作键名与参数收进折叠的技术明细。
 * 提交后不执行——L2 进待审批队列由值班批准，L3 只留提案。
 */
import { computed, ref } from 'vue'
import { proposeRagAction, type RagAction } from '../api/ragAdmin'
import RagActionDetail from './RagActionDetail.vue'
import { label_action, label_risk, label_status } from '../utils/ragLabels'

const props = defineProps<{
  actions: RagAction[]
  selected: RagAction | null
}>()

const emit = defineEmits<{
  open: [actionId: number]
  close: []
  approve: [actionId: number]
  cancel: [actionId: number]
  runWorker: []
  refresh: []
}>()

/** 可提议的业务动作：一个选项锁定动作键与目标字段，避免操作者拼参数。 */
interface ActionOption {
  id: string
  key: string
  field: 'username' | 'channel_code'
  label: string
  targetLabel: string
  /** 开关类动作才有目标态；钱包提案没有。 */
  active?: boolean
}

const OPTIONS: ActionOption[] = [
  {
    id: 'user_disable', key: 'set_user_active', active: false,
    field: 'username', label: '停用用户账号', targetLabel: '用户账号（登录名或手机号）',
  },
  {
    id: 'user_enable', key: 'set_user_active', active: true,
    field: 'username', label: '恢复用户账号', targetLabel: '用户账号（登录名或手机号）',
  },
  {
    id: 'channel_off', key: 'set_channel_active', active: false,
    field: 'channel_code', label: '关闭支付渠道', targetLabel: '渠道编码',
  },
  {
    id: 'channel_on', key: 'set_channel_active', active: true,
    field: 'channel_code', label: '开启支付渠道', targetLabel: '渠道编码',
  },
  {
    id: 'wallet', key: 'propose_wallet_adjustment', field: 'username',
    label: '提交余额调整建议（不改资金）', targetLabel: '用户账号（登录名或手机号）',
  },
]

const optionId = ref<string>('user_disable')
const target = ref('')
const amount = ref('')
const reason = ref('')
const busy = ref(false)
const error = ref('')
const notice = ref('')
const showDetail = ref(false)

const option = computed(
  () => OPTIONS.find((item) => item.id === optionId.value) ?? OPTIONS[0],
)
const isWallet = computed(() => option.value.key === 'propose_wallet_adjustment')
const canSubmit = computed(
  () => !busy.value
    && target.value.trim().length > 0
    && reason.value.trim().length >= 4
    && (!isWallet.value || amount.value.trim().length > 0),
)

/** 提交与技术明细共用同一份参数，避免两处各拼一遍对不上。 */
const requestParams = computed<Record<string, string | number | boolean>>(() => {
  const params: Record<string, string | number | boolean> = {}
  params[option.value.field] = target.value.trim()
  if (isWallet.value) {
    const value = Number(amount.value)
    if (amount.value.trim() && Number.isFinite(value) && value !== 0) {
      params.amount = value
    }
  } else {
    params.active = option.value.active === true
  }
  return params
})

async function submit(): Promise<void> {
  error.value = ''
  notice.value = ''
  if (isWallet.value && requestParams.value.amount === undefined) {
    error.value = '请填写非零的调整金额（元，可为负数表示扣减）'
    return
  }
  busy.value = true
  try {
    const action = await proposeRagAction(
      option.value.key, requestParams.value, reason.value.trim())
    notice.value = action.status === 'proposed'
      ? '提案已登记，等待工程评审；控制面不会执行它。'
      : `已提交，等待审批（编号 ${action.action_id}）。批准后台才会执行，可回滚。`
    target.value = ''
    amount.value = ''
    reason.value = ''
    emit('refresh')
  } catch (err) {
    error.value = err instanceof Error ? err.message : '提交失败'
  } finally {
    busy.value = false
  }
}

function verdictText(item: RagAction): string {
  if (item.verification?.passed === true) return '通过'
  if (item.verification?.passed === false) return '失败'
  return '-'
}
</script>

<template>
  <section class="workspace">
    <div class="stack">
      <section class="panel propose-panel">
        <div class="panel-head"><h2>提议一个操作</h2></div>
        <label class="field">
          <span>要做什么</span>
          <select v-model="optionId">
            <option v-for="item in OPTIONS" :key="item.id" :value="item.id">{{ item.label }}</option>
          </select>
        </label>
        <label class="field">
          <span>{{ option.targetLabel }}</span>
          <input v-model="target" type="text" maxlength="64" placeholder="例如 138****0000" />
        </label>
        <label v-if="isWallet" class="field">
          <span>调整金额（元，负数为扣减）</span>
          <input v-model="amount" type="text" maxlength="12" placeholder="例如 -50" />
        </label>
        <label class="field">
          <span>理由（会写进审计与动作记录）</span>
          <textarea v-model="reason" rows="2" maxlength="500" placeholder="至少 4 个字，说清为什么" />
        </label>
        <button v-if="!showDetail" class="text-button" type="button" @click="showDetail = true">
          展开技术明细（动作键与参数）
        </button>
        <dl v-else class="tech">
          <dt>动作键</dt><dd>{{ option.key }}</dd>
          <dt>参数</dt><dd>{{ JSON.stringify(requestParams) }}</dd>
        </dl>
        <div class="submit-row">
          <button class="primary" type="button" :disabled="!canSubmit" @click="submit">
            {{ busy ? '提交中...' : '提交' }}
          </button>
          <p v-if="notice" class="hint">{{ notice }}</p>
          <p v-if="error" class="hint danger">{{ error }}</p>
        </div>
        <p class="hint">
          停用账号会让该账号立即无法登录（最长约 1 分钟内全网生效）；
          余额调整只是一份提案，不会改动资金。
        </p>
      </section>

      <section class="panel table-panel">
        <div class="panel-head">
          <h2>修复动作</h2>
          <button class="text-button" type="button" @click="emit('runWorker')">执行队列</button>
        </div>
        <table>
          <thead>
            <tr><th>编号</th><th>事件</th><th>动作</th><th>级别</th><th>状态</th><th>执行者</th><th>结果</th><th></th></tr>
          </thead>
          <tbody>
            <tr v-for="item in props.actions" :key="item.action_id" @click="emit('open', item.action_id)">
              <td>{{ item.action_id }}</td><td>{{ item.incident_id ?? '-' }}</td>
              <td>{{ label_action(item.action_key) }}</td>
              <td>{{ label_risk(item.risk_level) }}</td>
              <td>{{ label_status(item.status) }}</td>
              <td>{{ item.executed_by || '-' }}</td>
              <td>{{ verdictText(item) }}</td>
              <td class="row-actions">
                <button v-if="item.status === 'waiting_approval'" @click.stop="emit('approve', item.action_id)">批准</button>
                <button
                  v-if="['proposed', 'waiting_approval', 'queued'].includes(item.status)"
                  class="danger"
                  @click.stop="emit('cancel', item.action_id)"
                >取消</button>
              </td>
            </tr>
            <tr v-if="!props.actions.length"><td colspan="8" class="empty">暂无动作</td></tr>
          </tbody>
        </table>
      </section>
    </div>

    <RagActionDetail
      v-if="props.selected"
      :action="props.selected"
      @close="emit('close')"
      @refresh="emit('refresh')"
    />
  </section>
</template>

<style scoped>
.workspace {
  display: grid;
  grid-template-columns: minmax(0, 1.7fr) minmax(320px, 1fr);
  gap: 14px;
  align-items: start;
}
.stack { display: grid; gap: 14px; }
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
  margin-bottom: 10px;
  color: var(--text-muted, #6b7280);
}
h2 { margin: 0; font-size: 14px; }
.field { display: grid; gap: 4px; margin-bottom: 8px; font-size: 13px; }
.field span { color: var(--text-muted, #6b7280); }
.field input, .field select, .field textarea {
  padding: 6px 8px;
  border: 1px solid var(--border-color, #d5dae3);
  border-radius: 6px;
  font: inherit;
}
.tech { display: grid; grid-template-columns: 72px 1fr; gap: 4px 8px; margin: 6px 0; font-size: 12px; }
.tech dt { color: var(--text-muted, #6b7280); }
.tech dd { margin: 0; overflow-wrap: anywhere; }
.submit-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
button.primary { background: #2563eb; color: #fff; border-color: #2563eb; }
button.danger { color: #b42318; }
button:disabled { opacity: .55; cursor: not-allowed; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 8px 6px; border-bottom: 1px solid var(--border-color, #e5e7eb); text-align: left; }
th { color: var(--text-muted, #6b7280); font-weight: 500; }
tbody tr { cursor: pointer; }
tbody tr:hover { background: rgba(37, 99, 235, 0.05); }
.row-actions { display: flex; gap: 6px; }
.row-actions button { min-height: 28px; padding: 3px 8px; }
.text-button { min-height: auto; padding: 0; border: 0; background: transparent; color: #1d4ed8; }
.hint { color: var(--text-muted, #6b7280); font-size: 12px; margin: 6px 0 0; }
.hint.danger { color: #b42318; }
.empty { color: var(--text-muted, #6b7280); }
.table-panel { overflow-x: auto; }
@media (max-width: 900px) {
  .workspace { grid-template-columns: 1fr; }
}
</style>
