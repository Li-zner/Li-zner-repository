<script setup lang="ts">
/** 绑定手机号弹窗：验证码校验 → 解锁额度（后端可能改绑用户名并签发新 token 对） */
import { onUnmounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { bindPhone, sendPhoneCode } from '../api/phone'
import { saveTokens } from '../api/http'
import { useAuthStore } from '../stores/auth'

const props = defineProps<{ tip: string }>()
const emit = defineEmits<{ close: []; bound: [] }>()
const { t } = useI18n()
const auth = useAuthStore()

const phone = ref('')
const code = ref('')
const error = ref('')
const errorOk = ref(false)
const countdown = ref(0)
const busy = ref(false)
const sending = ref(false)
let timer: ReturnType<typeof setInterval> | null = null

async function sendCode() {
  if (countdown.value > 0 || sending.value) return
  error.value = ''
  sending.value = true
  try {
    await sendPhoneCode(phone.value)
    errorOk.value = true
    error.value = t('code_sent')
    countdown.value = 60
    timer = setInterval(() => {
      countdown.value -= 1
      if (countdown.value <= 0 && timer) clearInterval(timer)
    }, 1000)
  } catch (e) {
    errorOk.value = false
    error.value = e instanceof Error ? e.message : t('send_failed_2')
  } finally {
    sending.value = false
  }
}

async function submit() {
  if (!phone.value || !code.value) {
    error.value = t('fill_phone_code')
    return
  }
  busy.value = true
  try {
    const result = await bindPhone(phone.value, code.value)
    saveTokens(result)
    // 绑定已经成功即为成功；profile 刷新失败只影响本地资料缓存，不能把
    // 一次已提交成功的绑定反转成“操作失败”。
    void auth.loadProfile().catch(() => {})
    emit('close')
    emit('bound')
  } catch (e) {
    errorOk.value = false
    error.value = e instanceof Error ? e.message : t('operation_failed')
  } finally {
    busy.value = false
  }
}

// 弹窗关闭（v-if 卸载）时清掉倒计时 interval
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>

<template>
  <div class="payment-overlay show">
    <div class="payment-modal" style="max-width:400px;">
      <div class="modal-header">
        <h3>{{ t('bind_phone_title') }}</h3>
      </div>
      <div class="modal-body">
        <div style="font-size:13px;color:var(--text-secondary);margin-bottom:16px;line-height:1.6;">{{ props.tip || t('bind_phone_trial_tip') }}</div>
        <input v-model="phone" type="tel" inputmode="numeric" maxlength="11" :placeholder="t('phone_input_ph')">
        <div style="display:flex;gap:6px;margin-bottom:14px;">
          <input v-model="code" type="text" inputmode="numeric" maxlength="6" :placeholder="t('code_ph')">
          <button :disabled="countdown > 0 || sending" style="width:110px;flex-shrink:0;padding:12px 0;background:var(--primary);color:#fff;border:none;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;white-space:nowrap;" @click="sendCode">
            {{ countdown > 0 ? `${countdown}s` : t('bind_phone_send_code') }}
          </button>
        </div>
        <div v-if="error" :style="{ fontSize: '13px', marginBottom: '10px', color: errorOk ? '#22c55e' : '#ef4444' }">{{ error }}</div>
        <div style="display:flex;gap:8px;">
          <button :disabled="busy" style="flex:1;padding:12px;background:var(--primary);color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;" @click="submit">{{ t('bind_phone_btn') }}</button>
          <button style="flex:1;padding:12px;background:var(--bg-body);color:var(--text-secondary);border:1.5px solid var(--border-color);border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;" @click="emit('close')">{{ t('skip_bind') }}</button>
        </div>
      </div>
    </div>
  </div>
</template>
