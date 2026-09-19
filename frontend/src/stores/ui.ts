/** UI 态（Pinia）：主题 + 全局弹窗开关 + 轻提示 */
import { defineStore } from 'pinia'
import { Theme, StorageKey } from '../enums'

function applyTheme(theme: Theme): void {
  document.body.setAttribute('data-theme', theme)
}

const THEMES = new Set<Theme>([Theme.Classic, Theme.Glass, Theme.Dark])

function read_theme(): Theme {
  try {
    const value = localStorage.getItem(StorageKey.Theme) as Theme | null
    return value && THEMES.has(value) ? value : Theme.Classic
  } catch {
    return Theme.Classic
  }
}

export const useUiStore = defineStore('ui', {
  state: () => ({
    theme: read_theme(),
    settingsOpen: false,
    walletOpen: false,
  }),
  actions: {
    initTheme() {
      applyTheme(this.theme)
    },
    setTheme(theme: Theme) {
      if (!THEMES.has(theme)) return
      this.theme = theme
      try {
        localStorage.setItem(StorageKey.Theme, theme)
      } catch {
        // 隐私模式或存储禁用时仅保留内存态。
      }
      applyTheme(theme)
    },
    openSettings() {
      this.settingsOpen = true
    },
    closeSettings() {
      this.settingsOpen = false
    },
    openWallet() {
      this.walletOpen = true
    },
    closeWallet() {
      this.walletOpen = false
    },
  },
})
