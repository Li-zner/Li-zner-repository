<script setup lang="ts">
/** 登录页：账密 / 手机号验证码（登录或注册）双标签 + GitHub OAuth + 回调令牌落地 */
import { onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useAuthStore } from '../stores/auth'
import { saveTokens } from '../api/http'
import { phoneLogin, phoneRegister, sendPhoneCode } from '../api/phone'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const tab = ref<'password' | 'phone'>('password')
const username = ref('')
const password = ref('')
const error = ref('')
const info = ref('')
const loading = ref(false)

// 手机号登录/注册
const phone = ref('')
const phoneCode = ref('')
const phonePassword = ref('')
const agree = ref(true)
const countdown = ref(0)
let timer: ReturnType<typeof setInterval> | null = null

function switchTab(name: 'password' | 'phone') {
  tab.value = name
  error.value = ''
  info.value = ''
}

async function submit() {
  error.value = ''
  loading.value = true
  try {
    await auth.login(username.value, password.value)
    router.push('/chat')
  } catch (e) {
    error.value = e instanceof Error ? e.message : t('login_error_2')
  } finally {
    loading.value = false
  }
}

async function handleSendCode() {
  error.value = ''
  info.value = ''
  if (countdown.value > 0) return
  try {
    await sendPhoneCode(phone.value)
    info.value = t('code_sent')
    countdown.value = 60
    timer = setInterval(() => {
      countdown.value -= 1
      if (countdown.value <= 0 && timer) clearInterval(timer)
    }, 1000)
  } catch (e) {
    error.value = e instanceof Error ? e.message : t('send_failed_2')
  }
}

async function handlePhoneLogin() {
  error.value = ''
  info.value = ''
  try {
    // 填了密码走注册（需勾选协议），否则纯验证码登录（自动注册）
    const result = phonePassword.value
      ? await phoneRegister(phone.value, phoneCode.value, phonePassword.value, agree.value)
      : await phoneLogin(phone.value, phoneCode.value)
    saveTokens(result)
    await auth.loadProfile()
    if (result.default_password_hint) window.alert(t('default_pwd_hint'))
    router.push('/chat')
  } catch (e) {
    error.value = e instanceof Error ? e.message : t('operation_failed')
  }
}

function goGithub() {
  window.location.href = '/auth/github'
}

// 离开页面时清掉倒计时 interval（否则僵尸定时器持有组件引用）
onUnmounted(() => { if (timer) clearInterval(timer) })

onMounted(() => {
  // GitHub 回调落地。后端 302 形态是 /?token=..&refresh_token=..——query 在 hash 之前，
  // hash 路由的 route.query 看不到它，必须直接读 document.location.search；
  // quota_limited / error 同理。
  const q = route.query
  const search = new URLSearchParams(window.location.search)
  const token = (typeof q.token === 'string' && q.token) || search.get('token') || ''
  const refresh =
    (typeof q.refresh_token === 'string' && q.refresh_token) || search.get('refresh_token') || ''
  if (token && refresh) {
    auth.loginWithTokens(token, refresh)
    saveTokens({ access_token: token, refresh_token: refresh, token_type: 'bearer' })
    // 抹掉文档级 query：token 不留在地址栏与历史记录
    window.history.replaceState(null, '', window.location.pathname)
    router.replace('/chat')
    return
  }
  const err = (typeof q.error === 'string' && q.error) || search.get('error') || ''
  if (err) {
    error.value =
      err === 'github_username_taken'
        ? t('github_username_taken')
        : `${t('login_failed_2')}${err}`
    if (search.has('error')) window.history.replaceState(null, '', window.location.pathname)
  }
})
</script>

<template>
  <div id="login-container">
    <div class="login-box">
      <h2>{{ t('app_title') }}</h2>
      <p>{{ t('login_subtitle') }}</p>

      <div class="login-tabs">
        <button
          type="button"
          class="login-tab"
          :class="{ active: tab === 'password' }"
          @click="switchTab('password')"
        >
          {{ t('tab_password') }}
        </button>
        <button
          type="button"
          class="login-tab"
          :class="{ active: tab === 'phone' }"
          @click="switchTab('phone')"
        >
          {{ t('tab_phone') }}
        </button>
      </div>

      <!-- 密码登录 -->
      <div v-if="tab === 'password'">
        <input
          v-model="username"
          type="text"
          :placeholder="t('username_ph')"
          autocomplete="username"
        >
        <input
          v-model="password"
          type="password"
          :placeholder="t('password_ph')"
          autocomplete="current-password"
        >
        <button type="button" class="btn-primary" :disabled="loading" @click="submit">
          {{ loading ? t('loading') : t('login_btn') }}
        </button>
        <div v-if="error" class="error-msg">{{ error }}</div>
      </div>

      <!-- 手机号登录 -->
      <div v-else>
        <input v-model="phone" type="text" :placeholder="t('phone_input_ph')">
        <div class="code-row">
          <input v-model="phoneCode" type="text" :placeholder="t('code_ph')">
          <button type="button" :disabled="countdown > 0" @click="handleSendCode">
            {{ countdown > 0 ? `${countdown}s` : t('get_code') }}
          </button>
        </div>
        <input
          v-model="phonePassword"
          type="password"
          :placeholder="t('phone_password_ph')"
        >
        <p class="field-hint">{{ t('phone_password_hint') }}</p>
        <label class="agree-row">
          <input v-model="agree" type="checkbox">
          <span>{{ t('agree_prefix') }}</span>
          <a href="/user-agreement.html" target="_blank">《用户协议》</a>
        </label>
        <button type="button" class="btn-primary" @click="handlePhoneLogin">{{ t('phone_login_btn') }}</button>
        <div v-if="error" class="error-msg">{{ error }}</div>
        <div v-if="info" class="info-msg">{{ info }}</div>
      </div>

      <!-- GitHub 登录 -->
      <div class="github-section">
        <p>{{ t('or_third_party') }}</p>
        <a href="/auth/github">
          <button type="button" @click="goGithub">{{ t('github_login') }}</button>
        </a>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 表单控件基础样式（从全局 app.css 收编：全局 ID 特异性兜底曾压过本组件 scoped 样式） */
.login-box input:not([type='checkbox']) {
  width: 100%;
  padding: 12px 14px;
  border: 1.5px solid var(--border-color);
  border-radius: var(--radius-sm);
  font-size: 15px;
  margin-bottom: 12px;
  background: #fafafa;
  transition: var(--transition);
  outline: none;
  box-sizing: border-box;
}
.login-box input:not([type='checkbox']):focus {
  border-color: var(--primary);
  box-shadow: 0 0 0 4px rgba(61, 122, 92, 0.12);
  background: #fff;
}
.btn-primary {
  width: 100%;
  padding: 12px;
  background: var(--primary);
  color: #fff;
  border: none;
  border-radius: var(--radius-sm);
  font-size: 15px;
  font-weight: 600;
  cursor: pointer;
  transition: var(--transition);
  margin-top: 4px;
}
.btn-primary:hover { background: var(--primary-dark); }
.btn-primary:disabled { opacity: 0.6; cursor: not-allowed; }

.login-tabs {
  display: flex;
  gap: 0;
  margin-bottom: 20px;
  border: 1px solid var(--border-color);
  border-radius: var(--radius-sm);
  overflow: hidden;
}
.login-tab {
  flex: 1;
  padding: 10px;
  background: #f0f5f0;
  color: var(--text-secondary);
  border: none;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  transition: 0.2s;
}
.login-tab.active {
  background: var(--primary);
  color: #fff;
}
.code-row {
  display: flex;
  gap: 6px;
  margin-bottom: 10px;
}
.code-row input {
  flex: 1;
  min-width: 0;
  padding: 12px 14px;
  border: 1.5px solid var(--border-color);
  border-radius: var(--radius-sm);
  font-size: 15px;
  background: #fafafa;
  transition: var(--transition);
  outline: none;
  box-sizing: border-box;
  margin-bottom: 0;
}
.code-row button {
  width: 92px;
  flex-shrink: 0;
  padding: 12px 0;
  background: var(--primary);
  color: #fff;
  border: none;
  border-radius: var(--radius-sm);
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
  transition: 0.2s;
  box-sizing: border-box;
}
.field-hint {
  margin: -6px 0 12px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--text-muted);
}
.agree-row {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 14px;
  font-size: 12px;
  color: var(--text-secondary);
  cursor: pointer;
  white-space: nowrap;
}
.agree-row input {
  width: 16px;
  height: 16px;
  flex-shrink: 0;
  margin: 0;
  cursor: pointer;
  accent-color: var(--primary);
}
.agree-row a {
  color: var(--primary);
  text-decoration: underline;
}
.github-section {
  margin-top: 20px;
  padding-top: 20px;
  border-top: 1px solid #e5e5e5;
  text-align: center;
}
.github-section p {
  color: #8e8e8e;
  font-size: 13px;
  margin-bottom: 12px;
}
.github-section a {
  text-decoration: none;
  display: block;
}
.github-section button {
  width: 100%;
  padding: 12px;
  background: #24292e;
  color: #fff;
  border: none;
  border-radius: 8px;
  font-size: 16px;
  font-weight: 500;
  cursor: pointer;
  transition: background 0.2s;
}
.github-section button:hover {
  background: #1b1f23;
}
.error-msg {
  color: #ef4444;
  font-size: 13px;
  margin-top: 10px;
}
.info-msg {
  color: var(--primary);
  font-size: 13px;
  margin-top: 10px;
}
</style>
