<script setup lang="ts">
/** 会话列表：置顶/切换/内联重命名/删除/导出（本地持久化，至少保留一个） */
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useChatStore } from '../stores/chat'
import { downloadSession } from '../utils/sessions'

const { t, locale } = useI18n()
const chat = useChatStore()

/** 正在重命名的会话 id 与草稿（不用 window.prompt：部分环境禁用原生弹窗） */
const renamingId = ref('')
const renameDraft = ref('')

function startRename(id: string, currentTitle: string) {
  renamingId.value = id
  renameDraft.value = currentTitle
}

function commitRename(id: string) {
  if (renameDraft.value.trim()) chat.renameSession(id, renameDraft.value)
  renamingId.value = ''
}

/** Enter 确认重命名（IME 组词确认的回车不触发） */
function onRenameEnter(e: KeyboardEvent, id: string) {
  if (e.isComposing || e.keyCode === 229) return
  commitRename(id)
}

function remove(id: string) {
  if (chat.sessions.length <= 1) {
    window.alert('至少保留一个会话')
    return
  }
  if (window.confirm('删除这个对话？')) chat.deleteSession(id)
}

/** 导出会话为 Markdown 文件 */
function exportSession(id: string) {
  const s = chat.sessions.find((x) => x.id === id)
  if (s) downloadSession(s, locale.value)
}
</script>

<template>
  <div class="history-list">
    <div
      v-for="s in chat.sortedSessions"
      :key="s.id"
      class="history-item"
      :class="{ active: s.id === chat.currentSessionId, pinned: s.pinned }"
      @click="chat.switchSession(s.id)"
    >
      <template v-if="renamingId === s.id">
        <!-- 内联重命名：Enter 确认 / Esc 取消 -->
        <input
          v-model="renameDraft"
          class="rename-input"
          :placeholder="t('rename')"
          @click.stop
          @keydown.enter.stop="onRenameEnter($event, s.id)"
          @keydown.esc.stop="renamingId = ''"
          @blur="commitRename(s.id)"
        >
      </template>
      <template v-else>
        <span class="item-name" :title="s.title">{{ s.pinned ? '📌 ' : '' }}{{ s.title }}</span>
        <button
          class="pin-btn"
          :title="s.pinned ? t('unpin_session') : t('pin_session')"
          @click.stop="chat.togglePin(s.id)"
        >{{ s.pinned ? '已顶' : '顶' }}</button>
        <button class="rename-btn" :title="t('rename')" @click.stop="startRename(s.id, s.title)">R</button>
        <button class="export-btn" :title="t('export_session')" @click.stop="exportSession(s.id)">E</button>
        <button class="delete-btn" :title="t('delete_btn')" @click.stop="remove(s.id)">D</button>
      </template>
    </div>
  </div>
</template>

<style scoped>
/* 四个操作按钮统一为等宽小块、垂直居中、右缘对齐（覆盖全局 min-width:28px 的松散分布） */
.history-item button {
  min-width: 24px;
  height: 24px;
  padding: 0 4px;
  margin: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 4px;
  font-size: 12px;
  line-height: 1;
}
/* 置顶会话底色强调（📌 前缀 + 浅主色底） */
.history-item.pinned {
  background: var(--primary-light);
}
.history-item.pinned .item-name {
  color: var(--primary-dark);
  font-weight: 600;
}
.pin-btn {
  font-size: 12px;
  color: var(--text-muted);
  background: none;
  border: none;
  cursor: pointer;
  padding: 0 4px;
  flex-shrink: 0;
}
.pin-btn:hover {
  color: var(--primary);
}
.export-btn:hover {
  color: var(--primary);
}
.rename-input {
  flex: 1;
  min-width: 0;
  height: 28px;
  border: 1px solid var(--primary);
  border-radius: 6px;
  background: #fff;
  color: var(--text-primary);
  font-size: 13px;
  padding: 0 8px;
  outline: none;
}
</style>
