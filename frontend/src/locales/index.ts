/** vue-i18n 入口：词典平移自旧 i18n.js；语言偏好持久化 localStorage（与旧键 travel_lang 兼容） */
import { createI18n } from 'vue-i18n'
import zhCN from './zh-CN'
import enUS from './en-US'

export type Locale = 'zh' | 'en'
const STORAGE_KEY = 'travel_lang'

export function currentLocale(): Locale {
  return (localStorage.getItem(STORAGE_KEY) as Locale) || 'zh'
}

export function setLocale(locale: Locale): void {
  localStorage.setItem(STORAGE_KEY, locale)
  i18n.global.locale.value = locale
}

const i18n = createI18n({
  legacy: false,
  locale: currentLocale(),
  fallbackLocale: 'zh',
  messages: { zh: zhCN, en: enUS },
  missingWarn: false,
  fallbackWarn: false,
})

export default i18n
