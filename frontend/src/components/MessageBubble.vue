<script setup lang="ts">
/** 单条消息：旧版 .message DOM——think-block-top 思考面板 + answer-box 正文 + msg-actions 操作 */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { renderMarkdown } from '../utils/markdown'
import type { ChatMessage, ToolLine } from '../stores/chat'

const props = defineProps<{
  message: ChatMessage
  index: number
  isLast: boolean
}>()
const emit = defineEmits<{
  delete: [index: number]
  rate: [index: number]
}>()

const { t } = useI18n()
const isUser = computed(() => props.message.role === 'user')
const html = computed(() => (isUser.value ? '' : renderMarkdown(props.message.content)))
const done = computed(() => isUser.value || (Boolean(props.message.content) && !props.message.streaming))

/** 思考面板行：先工具调用/结果，后推理文本（旧版结构） */
const thinkLines = computed<ToolLine[]>(() => {
  if (isUser.value) return []
  const lines: ToolLine[] = []
  for (const tool of props.message.tools ?? []) lines.push(tool)
  for (const text of (props.message.reasoning ?? '').split('\n')) {
    if (text.trim()) lines.push({ kind: 'text', text })
  }
  return lines
})

const timeText = computed(() => {
  if (!props.message.timestamp) return ''
  const d = new Date(props.message.timestamp)
  return `${d.getMonth() + 1}.${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
})

async function copyAll() {
  const m = props.message
  const text = m.content + (m.reasoning ? `\n\n--- ${t('think_process')} ---\n${m.reasoning}` : '')
  await navigator.clipboard?.writeText(text).catch(() => {})
}
</script>

<template>
  <div class="message" :class="message.role">
    <!-- 流式生成中默认展开（可实时看思考），完成后自动折叠；用户手动开合不被覆盖 -->
    <details
      v-if="!isUser && thinkLines.length"
      class="think-block-top"
      :open="message.streaming"
    >
      <summary>{{ t('think_process') }}</summary>
      <div v-for="(line, i) in thinkLines" :key="i" :class="'think-' + line.kind">{{ line.text }}</div>
    </details>

    <div v-if="isUser && message.files?.length" class="file-attach">
      <span v-for="f in message.files" :key="f">{{ t('attachment_prefix') }}{{ f }}</span>
    </div>

    <div v-if="isUser" class="answer-box">{{ message.content }}</div>
    <div v-else class="answer-box" v-html="html"></div>


    <div class="bottom-row">
      <span class="msg-time">{{ timeText }}</span>
      <span v-if="done" class="msg-actions">
        <button :title="t('copy_content')" @click.stop="copyAll">{{ t('copy_short') }}</button>
        <template v-if="!isUser">
          <button :title="t('delete_this_qa')" @click.stop="emit('delete', index)">{{ t('delete_short') }}</button>
          <button v-if="!message.rated" :title="t('rate_answer')" @click.stop="emit('rate', index)">{{ t('rate_short') }}</button>
        </template>
      </span>
    </div>
  </div>
</template>

<style scoped>
.file-attach {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  font-size: 12px;
  margin-top: 8px;
  padding-top: 6px;
  border-top: 1px dashed rgb(255 255 255 / 30%);
}
.file-attach span {
  background: rgb(255 255 255 / 25%);
  padding: 2px 8px;
  border-radius: 10px;
}
.bottom-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 6px;
}
</style>
