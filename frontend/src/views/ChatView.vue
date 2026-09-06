<script setup lang="ts">
/** 聊天主界面：旧版 .app 骨架。默认旅游人格；顶部求职/民法典按钮切换人格 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useAuthStore } from '../stores/auth'
import { useChatStore } from '../stores/chat'
import { useUiStore } from '../stores/ui'
import { uploadFile, type UploadedFile } from '../api/files'
import { rateMessage } from '../api/user'
import { getWallet } from '../api/payment'
import MessageBubble from '../components/MessageBubble.vue'
import SessionList from '../components/SessionList.vue'
import SettingsModal from '../components/SettingsModal.vue'
import WalletModal from '../components/WalletModal.vue'
import TxModal from '../components/TxModal.vue'
import BindPhoneModal from '../components/BindPhoneModal.vue'

const { t } = useI18n()
const router = useRouter()
const auth = useAuthStore()
const chat = useChatStore()
const ui = useUiStore()

const input = ref('')
const uploads = ref<UploadedFile[]>([])
const uploadError = ref('')
const balance = ref<number | null>(null)
const rateIndex = ref<number | null>(null)
const txOpen = ref(false)
const drawerOpen = ref(false)
/** 顶部栏收起/展开（小三角切换） */
const headerCollapsed = ref(false)

// ---------- 滚动跟随：流式时贴底自动滚，用户上翻则停手 + 悬浮"回到底部" ----------
const messagesEl = ref<HTMLElement | null>(null)
const inputEl = ref<HTMLTextAreaElement | null>(null)
const showScrollBottom = ref(false)
const nearBottom = ref(true)

function onMessagesScroll() {
  const el = messagesEl.value
  if (!el) return
  const dist = el.scrollHeight - el.scrollTop - el.clientHeight
  nearBottom.value = dist < 120
  showScrollBottom.value = dist > 300
}

function scrollToBottom() {
  const el = messagesEl.value
  if (el) el.scrollTop = el.scrollHeight
}

// 流式增量（正文/思考/工具行）或消息条数变化时，贴底状态才自动跟随
watch(
  () => {
    const msgs = chat.messages
    const last = msgs[msgs.length - 1]
    return `${chat.currentSessionId}|${msgs.length}|${last?.content.length ?? 0}|${last?.reasoning?.length ?? 0}|${last?.tools?.length ?? 0}`
  },
  async () => {
    if (!nearBottom.value) return
    await nextTick()
    scrollToBottom()
  },
)

// 会话切换/首屏渲染后贴底
watch(() => chat.currentSessionId, async () => {
  await nextTick()
  scrollToBottom()
})

// ---------- 多行输入：Enter 发送 / Shift+Enter 换行 / 自动增高 ----------
function autoGrow() {
  const el = inputEl.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 132) + 'px'
}

onMounted(async () => {
  if (!auth.profileLoaded) auth.loadProfile().catch(() => router.push('/login'))
  // 会话列表必须先加载完成，autoPrompt 才有 currentSession 可发（否则 send 静默返回 → 无输出）
  await chat.loadPersonas().catch(() => {})
  refreshBalance()
  if (chat.autoPrompt) {
    const prompt = chat.autoPrompt
    chat.setAutoPrompt('')
    await chat.send(prompt)
  }
})

// 人格切换时清空输入与附件（会话数据由 store 响应式切换，无需销毁组件）
watch(() => chat.currentPersonaId, () => {
  input.value = ''
  uploads.value = []
})

async function refreshBalance() {
  try { balance.value = (await getWallet()).balance } catch { /* 静默 */ }
}

async function onFilePicked(e: Event) {
  const target = e.target as HTMLInputElement
  const file = target.files?.[0]
  if (!file) return
  uploadError.value = ''
  try { uploads.value.push(await uploadFile(file)) }
  catch (err) { uploadError.value = err instanceof Error ? err.message : t('upload_failed') }
  finally { target.value = '' }
}

function clearUploads() { uploads.value = [] }

/** textarea Enter 发送 / Shift+Enter 换行；IME 组词确认的回车走默认确认行为 */
function onEnter(e: KeyboardEvent) {
  if (e.shiftKey) return
  if (e.isComposing || e.keyCode === 229) return
  e.preventDefault()
  void doSend()
}

async function doSend() {
  if (chat.streaming) { chat.cancelStream(); return }
  const text = input.value.trim()
  if (!text && !uploads.value.length) return
  const files = uploads.value.map((u) => u.filename)
  const ids = uploads.value.map((u) => u.file_id)
  // 立即清空：流式可持续数十秒，已发送内容挂在输入框里像没发出去
  input.value = ''
  uploads.value = []
  if (inputEl.value) inputEl.value.style.height = 'auto'
  await chat.send(text, ids)
  const lastUser = [...chat.messages].reverse().find((m) => m.role === 'user')
  if (lastUser && files.length) lastUser.files = files
  void refreshBalance()
}

async function doLogout() { await auth.logout(); router.push('/login') }

function newConversation() {
  chat.newConversation()
  uploads.value = []
  drawerOpen.value = false
}

function onDelete(index: number) {
  if (window.confirm(t('delete_this_qa'))) chat.deleteQA(index)
}

// ---------- 评分 ----------
const rateStars = ref(0)
function openRate(index: number) { rateIndex.value = index; rateStars.value = 0 }

async function submitRate(star: number) {
  if (rateIndex.value === null || !star) return
  const session = chat.currentSession
  const idx = rateIndex.value
  const userMsg = session?.messages[idx - 1]
  try {
    await rateMessage({
      rating: star,
      session_id: session?.conversationId ?? '',
      user_message: userMsg?.content ?? '',
      assistant_message: chat.messages[idx]?.content ?? '',
    })
    chat.markRated(idx)
  } catch {
    // 评分失败关闭弹窗但不标记已评（可重新评），不打断聊天
  }
  rateIndex.value = null
}

// ---------- 人格切换（顶部按钮：求职 / 民法典） ----------
const isMePersona = computed(() => chat.currentPersonaId === 'me')
const isCivilPersona = computed(() => chat.currentPersonaId === 'civil_code')
const isTravelPersona = computed(() => !isMePersona.value && !isCivilPersona.value)

function switchPersona(id: string) {
  if (chat.streaming) return
  chat.switchPersona(id)
}

/** 追问建议直发（不经 input 框） */
async function sendDirect(s: string) {
  await chat.send(s)
}

const mePresets = computed(() => [
  t('me_preset_1'), t('me_preset_2'), t('me_preset_3'),
  t('me_preset_4'), t('me_preset_5'), t('me_preset_6'), t('me_preset_7'),
])

const themes = [
  { key: 'classic', label: 'theme_classic' },
  { key: 'glass', label: 'theme_glass' },
  { key: 'dark', label: 'theme_dark' },
]

/** 预设问题 / 追问建议直发（不经 input 框） */
async function send(q: string) {
  await chat.send(q)
}
</script>

<template>
  <div class="app show">
    <div
      v-if="drawerOpen"
      class="sidebar-backdrop"
      @click="drawerOpen = false"
    ></div>
    <aside
      class="sidebar"
      :class="{ 'drawer-open': drawerOpen }"
    >
      <div class="sidebar-title">
        <span>{{ t('sidebar_title_1') }}</span>
        <span>{{ t('sidebar_title_2') }}</span>
        <button
          class="drawer-close"
          @click="drawerOpen = false"
        >✕</button>
      </div>
      <button
        class="new-chat-btn"
        @click="() => { newConversation(); drawerOpen = false }"
      >{{ t('new_chat') }}</button>

      <div class="history-label">{{ t('history_label') }}</div>
      <SessionList />

      <!-- 主题 -->
      <div class="sidebar-section">
        <div class="section-label">{{ t('theme_label') }}</div>
        <div class="theme-picker">
          <button
            v-for="th in themes"
            :key="th.key"
            class="theme-btn-pc"
            :class="{ active: ui.theme === th.key }"
            @click="ui.setTheme(th.key as never)"
          >{{ t(th.label) }}</button>
        </div>
      </div>

      <div class="sidebar-footer">
        <div>{{ t('sessions_saved_local') }}</div>
        <button @click="ui.openSettings()">⚙ {{ t('settings') }}</button>
        <!-- 退出入口：移动端 header 无退出按钮，收在此处 -->
        <button @click="doLogout">{{ t('nav_logout') }}</button>
      </div>
    </aside>

    <div class="chat-area">
      <!-- 收起/展开区：小三角骑在矩形下缘中部，随矩形一起上收（collapsed 时矩形高度塌为 0，三角贴顶） -->
      <div class="header-collapse-zone" :class="{ collapsed: headerCollapsed }">
      <div class="chat-header">
        <div class="header-left">
          <button class="chat-hamburger" @click="drawerOpen = true">☰</button>
          <button class="wallet-btn" @click="ui.openWallet()">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="1" y="5" width="22" height="14" rx="2"/><circle cx="12" cy="12" r="3"/><path d="M16 12h4"/></svg>
            <span class="balance-label">{{ t('balance') }}</span>
            <span class="balance-text">{{ balance !== null ? `¥${balance.toFixed(2)}` : '—' }}</span>
          </button>
          <span
            v-if="auth.user?.quota_limited && (auth.user?.remaining_questions ?? 0) >= 0"
            class="quota-badge"
            @click="chat.bindPhoneTip = t('bind_phone_trial_tip')"
          >{{ t('quota_badge', { n: auth.user?.remaining_questions }) }}</span>
        </div>
        <div class="header-actions">
          <router-link to="/map" class="map-btn">{{ t('map') }}</router-link>
          <button
            class="mode-btn"
            :class="{ active: isTravelPersona }"
            @click="switchPersona('unified')"
          >{{ t('travel_mode') }}</button>
          <button
            class="mode-btn"
            :class="{ active: isMePersona }"
            @click="switchPersona('me')"
          >{{ t('me_mode') }}</button>
          <button
            class="mode-btn"
            :class="{ active: isCivilPersona }"
            @click="switchPersona('civil_code')"
          >{{ t('civil_mode') }}</button>
          <button class="logout-btn" @click="doLogout">{{ t('nav_logout') }}</button>
        </div>
      </div>
      <!-- 展开态 ▴=点击收起；收起态 ▾=点击下拉。骑缝定位在 header 下缘中部 -->
      <button
        class="header-toggle"
        :title="headerCollapsed ? t('header_expand') : t('header_collapse')"
        @click="headerCollapsed = !headerCollapsed"
      >{{ headerCollapsed ? '▾' : '▴' }}</button>
      </div>

      <div
        ref="messagesEl"
        class="chat-messages"
        @scroll.passive="onMessagesScroll"
      >
        <template v-if="chat.messages.length === 0">
          <!-- 求职人格欢迎卡 -->
          <div v-if="isMePersona" class="welcome-card">
            <h1>{{ t('me_welcome_title') }}</h1>
            <div class="subtitle">{{ t('me_welcome_subtitle') }}</div>
            <div class="info-grid">
              <div><span class="label">{{ t('me_label_intent') }}</span><br><span class="value">{{ t('me_value_intent') }}</span></div>
              <div><span class="label">{{ t('me_label_edu') }}</span><br><span class="value">{{ t('me_value_edu') }}</span></div>
              <div><span class="label">{{ t('me_label_project') }}</span><br><span class="value">{{ t('me_value_project') }}</span></div>
              <div><span class="label">{{ t('me_label_speed') }}</span><br><span class="value">{{ t('me_value_speed') }}</span></div>
            </div>
            <div class="tag-list">
              <span v-for="tag in ['Python', 'FastAPI', 'Multi-Agent', 'RAG', 'Docker', 'DeepSeek', 'PostgreSQL', 'Redis']" :key="tag" class="tag">{{ tag }}</span>
            </div>
            <div class="hint">{{ t('me_contact') }}</div>
            <div class="hint">{{ t('me_hint') }}</div>
            <div class="presets">
              <button v-for="q in mePresets" :key="q" @click="send(q)">{{ q }}</button>
            </div>
          </div>
          <!-- 民法典人格欢迎卡（问题八） -->
          <div v-else-if="isCivilPersona" class="welcome-card">
            <h1>{{ t('civil_welcome_title') }}</h1>
            <div class="subtitle">{{ t('civil_welcome_subtitle') }}</div>
            <div class="hint">{{ t('civil_hint') }}</div>
            <div class="presets">
              <button @click="send('离婚冷静期是多久')">{{ t('civil_preset_1') }}</button>
              <button @click="send('借钱不还怎么办')">{{ t('civil_preset_2') }}</button>
              <button @click="send('高空抛物由谁负责')">{{ t('civil_preset_3') }}</button>
            </div>
          </div>
          <div v-else class="empty-state">{{ t('welcome') }}</div>
        </template>
        <MessageBubble
          v-for="(m, i) in chat.messages"
          :key="m.id ?? i"
          :message="m"
          :index="i"
          :is-last="i === chat.messages.length - 1"
          @delete="onDelete"
          @rate="openRate"
          @suggest="sendDirect"
        />
      </div>

      <!-- 回到底部悬浮键：上翻离开底部时出现 -->
      <Transition name="fadeup">
        <button
          v-if="showScrollBottom"
          class="scroll-bottom-btn"
          :title="t('scroll_bottom')"
          :aria-label="t('scroll_bottom')"
          @click="scrollToBottom"
        >↓</button>
      </Transition>

      <div v-if="uploads.length" class="file-indicator uploading">
        <div class="file-progress">
          <svg viewBox="0 0 24 24">
            <circle class="track" cx="12" cy="12" r="10"/>
            <circle class="fill-arc" cx="12" cy="12" r="10" stroke-dasharray="62.83" stroke-dashoffset="0"/>
          </svg>
          <span class="icon-overlay">📎</span>
        </div>
        <span class="file-label">{{ uploads[0].filename }}（{{ uploads[0].text_length }} {{ t('chars') }}）</span>
        <button class="file-close" @click="clearUploads">✕</button>
      </div>

      <div class="chat-input-area">
        <label class="upload-btn" :title="t('upload_title')">
          <input type="file" @change="onFilePicked">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
        </label>
        <div class="input-wrapper">
          <textarea
            ref="inputEl"
            v-model="input"
            rows="1"
            :placeholder="isMePersona ? t('input_ph_me') : t('input_ph')"
            @keydown.enter="onEnter"
            @input="autoGrow"
          ></textarea>
        </div>
        <button :class="{ 'btn-cancel': chat.streaming }" @click="doSend">
          {{ chat.streaming ? t('cancel_stream') : t('send') }}
        </button>
      </div>

      <SettingsModal v-if="ui.settingsOpen" />
      <WalletModal
        v-if="ui.walletOpen"
        @refresh-balance="refreshBalance"
        @open-tx="ui.closeWallet(); txOpen = true"
      />
      <TxModal v-if="txOpen" @close="txOpen = false" />
      <BindPhoneModal
        v-if="chat.bindPhoneTip"
        :tip="chat.bindPhoneTip"
        @close="chat.bindPhoneTip = ''"
        @bound="refreshBalance"
      />
      <div v-if="rateIndex !== null" class="payment-overlay show">
        <div class="payment-modal" style="max-width:380px;text-align:center;">
          <div class="modal-header">
            <h3>{{ t('rate_answer') }}</h3>
            <button class="close-btn" @click="rateIndex = null">✕</button>
          </div>
          <div class="modal-body" style="padding:24px 20px;">
            <div style="font-size:14px;color:var(--text-secondary);margin-bottom:16px;">{{ t('rate_choose') }}</div>
            <div style="display:flex;justify-content:center;gap:8px;font-size:34px;cursor:pointer;">
              <span
                v-for="star in 5"
                :key="star"
                style="transition:all 0.15s;"
                :style="{ color: star <= rateStars ? '#f5a623' : '#ddd' }"
                @click="() => { rateStars = star; submitRate(star) }"
              >★</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>


<style scoped>
/* 回到底部悬浮键（chat-area 定位锚点） */
.chat-area {
  position: relative;
}
.scroll-bottom-btn {
  position: absolute;
  right: 18px;
  bottom: 96px;
  width: 44px;
  height: 44px;
  border-radius: 50%;
  border: 1px solid var(--border-color);
  background: var(--bg-card);
  color: var(--text-secondary);
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
  box-shadow: var(--shadow-md);
  z-index: 30;
  display: flex;
  align-items: center;
  justify-content: center;
}
.scroll-bottom-btn:hover {
  color: var(--primary);
  border-color: var(--primary);
}
.fadeup-enter-active,
.fadeup-leave-active {
  transition: opacity 0.2s, transform 0.2s;
}
.fadeup-enter-from,
.fadeup-leave-to {
  opacity: 0;
  transform: translateY(8px);
}
.quota-badge {
  margin-left: 8px;
  padding: 6px 10px;
  background: rgb(245 158 11 / 12%);
  border: 1px solid rgb(245 158 11 / 40%);
  border-radius: 20px;
  color: #d97706;
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
  cursor: pointer;
  vertical-align: middle;
}
.civil-btn {
  background: var(--bg-body);
  border: 1px solid var(--border-color);
  color: var(--text-secondary);
  padding: 6px 14px;
  border-radius: 30px;
  font-size: 13px;
  cursor: pointer;
  transition: var(--transition);
}
.civil-btn:hover {
  background: var(--primary-light);
  border-color: var(--primary);
  color: var(--primary);
}
.empty-state {
  text-align: center;
  color: var(--text-muted);
  margin-top: 40vh;
}
</style>
<style scoped>
/* ---------- 顶部栏收起/展开（小三角骑缝在矩形下缘中部） ---------- */
.header-collapse-zone {
  position: relative;
  z-index: 10;
}
.header-collapse-zone .chat-header {
  max-height: 140px;
  overflow: hidden;
  transition: max-height 0.3s ease, padding 0.3s ease;
}
.header-collapse-zone.collapsed .chat-header {
  max-height: 0;
  padding-top: 0;
  padding-bottom: 0;
  border-bottom-width: 0;
}
/* 小三角：骑在 header 下缘（top:100% 减自身高一半），收起后随矩形贴到屏幕顶部。
   靠右放置：手机顶部中央是前置摄像头/挖孔区，放中间会被遮挡且难点按 */
.header-toggle {
  position: absolute;
  top: calc(100% - 9px);
  right: 16px;
  width: 44px;
  height: 18px;
  border: 1px solid var(--border-color);
  border-top: none;
  border-radius: 0 0 12px 12px;
  background: var(--header-bg, rgba(255, 255, 255, 0.92));
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1;
  cursor: pointer;
  z-index: 11;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: color 0.2s;
}
.header-toggle:hover {
  color: var(--primary);
}
</style>
