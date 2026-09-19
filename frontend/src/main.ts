import { createApp } from 'vue'
import { createPinia } from 'pinia'
import i18n from './locales'
import App from './App.vue'
import router from './router'
import './styles/app.css'
import './styles/adapt.css'

;(window as unknown as { __vueErrs: string[] }).__vueErrs = []
const app = createApp(App)
// 渲染错误持久化捕获：切人格空白问题的排查钩子（定位后可移除）
app.config.errorHandler = (err, _instance, info) => {
  console.error('[VueErrorHandler]', info, err)
  ;(window as unknown as { __vueErrs: string[] }).__vueErrs?.push(
    `[${info}] ${err instanceof Error ? err.stack?.slice(0, 500) : String(err)}`,
  )
}
app.use(createPinia()).use(router).use(i18n)
try {
  app.mount('#app')
} catch (e) {
  // mount 失败可见化：写到 DOM 与 title（生产排查钩子）
  document.title = '[MOUNT FAIL] ' + (e instanceof Error ? e.message : String(e))
  document.getElementById('app')?.setAttribute('data-mount-error', e instanceof Error ? e.stack?.slice(0, 300) ?? '' : '')
  throw e
}

// PWA Service Worker：仅生产构建注册（vite dev 不注册避免缓存干扰）；失败静默不影响功能
if ('serviceWorker' in navigator && import.meta.env.PROD) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {})
  })
}
