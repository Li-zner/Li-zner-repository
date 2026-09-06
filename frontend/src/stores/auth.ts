/** 认证态（Pinia）：登录态 / 用户信息 / GitHub 回调令牌接收入口 */
import { defineStore } from 'pinia'
import {
  clearTokens, getRefreshToken, saveTokens,
} from '../api/http'
import { fetchProfile, login as apiLogin, logout as apiLogout, type UserProfile } from '../api/auth'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    user: null as UserProfile | null,
    profileLoaded: false,
  }),
  actions: {
    /** 账密登录（含手机号作为账号，后端自动解析） */
    async login(username: string, password: string) {
      const pair = await apiLogin(username, password)
      saveTokens(pair)
      await this.loadProfile()
    },
    /** GitHub OAuth 回调落地：后端重定向 URL 上携带 token 对 */
    loginWithTokens(access: string, refresh: string) {
      saveTokens({ access_token: access, refresh_token: refresh, token_type: 'bearer' })
      return this.loadProfile()
    },
    async loadProfile() {
      this.user = await fetchProfile()
      this.profileLoaded = true
    },
    async logout() {
      const refresh = getRefreshToken()
      if (refresh) await apiLogout(refresh)
      clearTokens()
      this.user = null
      this.profileLoaded = false
    },
  },
})
