<script setup lang="ts">
/** 设置弹窗：账号 / 修改密码 / 语言（GLM 网页版风格：分区卡片 + 浅底输入 + 主色 CTA） */
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { changePassword } from '../api/user'
import { setLocale } from '../locales'
import { useAuthStore } from '../stores/auth'
import { useUiStore } from '../stores/ui'
import { Theme } from '../enums'

const { t, locale } = useI18n()
const auth = useAuthStore()
const ui = useUiStore()

const oldPwd = ref('')
const newPwd = ref('')
const confirmPwd = ref('')
const msg = ref('')
const msgOk = ref(false)
const busy = ref(false)

async function save() {
  msg.value = ''
  if (!oldPwd.value || !newPwd.value || !confirmPwd.value) {
    msgOk.value = false
    msg.value = t('fill_pwd_all')
    return
  }
  if (newPwd.value !== confirmPwd.value) {
    msgOk.value = false
    msg.value = t('pwd_mismatch')
    return
  }
  if (newPwd.value.length < 6) {
    msgOk.value = false
    msg.value = t('pwd_too_short')
    return
  }
  busy.value = true
  try {
    await changePassword(oldPwd.value, newPwd.value)
    msgOk.value = true
    msg.value = t('pwd_changed')
    oldPwd.value = newPwd.value = confirmPwd.value = ''
  } catch (e) {
    msgOk.value = false
    msg.value = e instanceof Error ? e.message : t('pwd_old_wrong')
  } finally {
    busy.value = false
  }
}

function pickTheme(theme: string) {
  ui.setTheme(theme as Theme)
}
</script>

<template>
  <div class="settings-overlay" @click.self="ui.closeSettings()">
    <div class="settings-modal">
      <!-- 头部 -->
      <header class="s-header">
        <div class="s-avatar">{{ (auth.user?.display_name || auth.user?.username || '?').slice(0, 1).toUpperCase() }}</div>
        <div class="s-user">
          <div class="s-name">{{ auth.user?.display_name || auth.user?.username }}</div>
          <div class="s-role">{{ auth.user?.role === 'admin' ? '管理员' : '用户' }}</div>
        </div>
        <button class="s-close" @click="ui.closeSettings()">✕</button>
      </header>

      <!-- 修改密码分区 -->
      <section class="s-section">
        <h4 class="s-section-title">{{ t('change_password') }}</h4>
        <div class="s-field">
          <label>{{ t('current_password') }}</label>
          <input v-model="oldPwd" type="password" autocomplete="current-password">
        </div>
        <div class="s-field">
          <label>{{ t('new_password') }}</label>
          <input v-model="newPwd" type="password" autocomplete="new-password">
        </div>
        <div class="s-field">
          <label>{{ t('confirm_password') }}</label>
          <input v-model="confirmPwd" type="password" autocomplete="new-password">
        </div>
        <p v-if="msg" class="s-msg" :class="{ ok: msgOk }">{{ msg }}</p>
        <button class="s-primary-btn" :disabled="busy" @click="save">
          {{ busy ? t('pwd_loading') : t('save_password') }}
        </button>
      </section>

      <!-- 偏好分区：主题 + 语言 -->
      <section class="s-section">
        <h4 class="s-section-title">{{ t('theme_label') }}</h4>
        <div class="s-seg">
          <button
            v-for="th in themes"
            :key="th.key"
            :class="{ active: ui.theme === th.key }"
            @click="pickTheme(th.key)"
          >{{ t(th.label) }}</button>
        </div>

        <h4 class="s-section-title" style="margin-top: 16px;">{{ t('language') }}</h4>
        <div class="s-seg">
          <button :class="{ active: locale === 'zh' }" @click="setLocale('zh')">中文</button>
          <button :class="{ active: locale === 'en' }" @click="setLocale('en')">English</button>
        </div>
      </section>
    </div>
  </div>
</template>

<script lang="ts">
const themes = [
  { key: 'classic', label: 'theme_classic' },
  { key: 'glass', label: 'theme_glass' },
  { key: 'dark', label: 'theme_dark' },
]
export default { data() { return { themes } } }
</script>

<style scoped>
.settings-overlay {
  position: fixed;
  inset: 0;
  background: rgb(0 0 0 / 45%);
  display: grid;
  place-items: center;
  z-index: 100;
  animation: fadeIn 0.15s ease;
}
.settings-modal {
  width: 400px;
  max-width: calc(100vw - 32px);
  max-height: 85vh;
  overflow-y: auto;
  background: #fff;
  border-radius: 16px;
  box-shadow: 0 20px 60px rgb(0 0 0 / 18%);
  padding: 0;
}
.s-header {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 20px 24px 16px;
  border-bottom: 1px solid #eef0f3;
}
.s-avatar {
  width: 40px;
  height: 40px;
  border-radius: 50%;
  background: var(--primary, #3d7a5c);
  color: #fff;
  display: grid;
  place-items: center;
  font-size: 17px;
  font-weight: 700;
}
.s-user { flex: 1; }
.s-name {
  font-size: 15px;
  font-weight: 600;
  color: #1f2329;
}
.s-role {
  font-size: 12px;
  color: #8f959e;
}
.s-close {
  border: none;
  background: none;
  font-size: 16px;
  color: #8f959e;
  cursor: pointer;
  padding: 4px 8px;
  border-radius: 6px;
}
.s-close:hover {
  background: #f2f3f5;
  color: #4e5969;
}
.s-section {
  padding: 16px 24px 20px;
  border-bottom: 1px solid #f2f3f5;
}
.s-section:last-child {
  border-bottom: none;
}
.s-section-title {
  font-size: 13px;
  font-weight: 600;
  color: #1f2329;
  margin: 0 0 12px;
}
.s-field {
  margin-bottom: 12px;
}
.s-field label {
  display: block;
  font-size: 12px;
  color: #8f959e;
  margin-bottom: 6px;
}
.s-field input {
  width: 100%;
  height: 40px;
  border: 1px solid #e5e6eb;
  border-radius: 8px;
  padding: 0 12px;
  font-size: 14px;
  background: #f7f8fa;
  box-sizing: border-box;
  transition: border-color 0.2s, background 0.2s;
}
.s-field input:focus {
  outline: none;
  border-color: var(--primary, #3d7a5c);
  background: #fff;
}
.s-primary-btn {
  width: 100%;
  height: 40px;
  border: none;
  border-radius: 8px;
  background: var(--primary, #3d7a5c);
  color: #fff;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.2s;
}
.s-primary-btn:hover:not(:disabled) {
  background: var(--primary-dark, #2c5f45);
}
.s-primary-btn:disabled {
  opacity: 0.55;
  cursor: default;
}
.s-seg {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
}
.s-seg button {
  height: 38px;
  border: 1px solid #e5e6eb;
  border-radius: 8px;
  background: #f7f8fa;
  color: #4e5969;
  font-size: 13px;
  cursor: pointer;
  transition: all 0.2s;
}
.s-seg button:hover {
  border-color: var(--primary, #3d7a5c);
  color: var(--primary, #3d7a5c);
}
.s-seg button.active {
  border-color: var(--primary, #3d7a5c);
  background: #fff;
  color: var(--primary, #3d7a5c);
  font-weight: 600;
}
.s-msg {
  font-size: 13px;
  margin: 0 0 10px;
}
.s-msg.ok { color: #00b42a; }
.s-msg:not(.ok) { color: #f53f3f; }
@keyframes fadeIn {
  from { opacity: 0; }
  to { opacity: 1; }
}
</style>
