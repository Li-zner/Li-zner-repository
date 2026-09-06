/** UI 态（Pinia）：主题 + 全局弹窗开关 + 轻提示 */
import { defineStore } from 'pinia'
import { Theme, StorageKey } from '../enums'

function applyTheme(theme: Theme): void {
  document.body.setAttribute('data-theme', theme)
}

export const useUiStore = defineStore('ui', {
  state: () => ({
    theme: (localStorage.getItem(StorageKey.Theme) as Theme) || Theme.Classic,
    settingsOpen: false,
    walletOpen: false,
  }),
  actions: {
    initTheme() {
      applyTheme(this.theme)
    },
    setTheme(theme: Theme) {
      this.theme = theme
      localStorage.setItem(StorageKey.Theme, theme)
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
