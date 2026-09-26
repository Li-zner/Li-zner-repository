<script setup lang="ts">
/** 空会话欢迎面板：按当前人格展示说明和快捷问题。 */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

const props = defineProps<{
  personaId: string
}>()

const emit = defineEmits<{
  send: [prompt: string]
}>()

const { t } = useI18n()
const isCivilPersona = computed(() => props.personaId === 'civil_code')
</script>

<template>
  <div v-if="isCivilPersona" class="welcome-card">
    <h1>{{ t('civil_welcome_title') }}</h1>
    <div class="subtitle">{{ t('civil_welcome_subtitle') }}</div>
    <div class="hint">{{ t('civil_hint') }}</div>
    <div class="presets">
      <button @click="emit('send', t('civil_preset_1'))">{{ t('civil_preset_1') }}</button>
      <button @click="emit('send', t('civil_preset_2'))">{{ t('civil_preset_2') }}</button>
      <button @click="emit('send', t('civil_preset_3'))">{{ t('civil_preset_3') }}</button>
    </div>
  </div>
  <div v-else class="empty-state">{{ t('welcome') }}</div>
</template>
