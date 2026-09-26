<script setup lang="ts">
/** 回答评分弹窗：提交后由父组件关闭。 */
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'

const emit = defineEmits<{
  close: []
  submit: [star: number]
}>()

const { t } = useI18n()
const rateStars = ref(0)

function chooseStar(star: number) {
  rateStars.value = star
  emit('submit', star)
}
</script>

<template>
  <div class="payment-overlay show">
    <div class="payment-modal rating-modal">
      <div class="modal-header">
        <h3>{{ t('rate_answer') }}</h3>
        <button
          class="close-btn"
          :aria-label="t('cancel')"
          @click="emit('close')"
        >
          ✕
        </button>
      </div>
      <div class="modal-body rating-body">
        <div class="rating-hint">{{ t('rate_choose') }}</div>
        <div class="rating-stars">
          <button
            v-for="star in 5"
            :key="star"
            class="star-btn"
            :class="{ active: star <= rateStars }"
            :aria-label="`${t('rate_answer')} ${star}`"
            @click="chooseStar(star)"
          >
            ★
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.rating-modal {
  max-width: 380px;
  text-align: center;
}

.rating-body {
  padding: 24px 20px;
}

.rating-hint {
  margin-bottom: 16px;
  color: var(--text-secondary);
  font-size: 14px;
}

.rating-stars {
  display: flex;
  justify-content: center;
  gap: 8px;
}

.star-btn {
  border: 0;
  background: none;
  color: #ddd;
  cursor: pointer;
  font-size: 34px;
  line-height: 1;
  transition: color 0.15s;
}

.star-btn.active {
  color: #f5a623;
}
</style>
