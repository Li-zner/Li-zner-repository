/** 独立 RAG 控制台入口，与主聊天页面分开构建和部署。 */
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import i18n from './locales'
import ConsoleApp from './ConsoleApp.vue'
import consoleRouter from './console-router'
import { setAuthMode } from './api/http'
import './styles/app.css'

setAuthMode('cookie')
const app = createApp(ConsoleApp)
app.use(createPinia()).use(consoleRouter).use(i18n)
try {
  app.mount('#app')
} catch (error) {
  const message = error instanceof Error ? error.message : String(error)
  document.title = '[CONSOLE MOUNT FAIL] ' + message
  document.getElementById('app')?.setAttribute('data-mount-error', message)
  throw error
}
