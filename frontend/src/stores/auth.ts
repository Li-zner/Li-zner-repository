/** 认证态（Pinia）：登录态 / 用户信息 / GitHub 回调令牌接收入口 */
import { defineStore } from 'pinia'
import {
  clearTokens, getRefreshToken, saveTokens,
} from '../api/http'
import {
  exchangeOAuthCode as apiExchangeOAuthCode,
  fetchProfile,
  login as apiLogin,
  logout as apiLogout,
  type UserProfile,
} from '../api/auth'

/** 同一时刻只保留一次 profile 拉取，避免首屏多个入口重复请求。 */
const profileLoads = new WeakMap<object, Promise<void>>()

export const useAuthStore = defineStore('auth', {
  state: () => ({
    user: null as UserProfile | null,
    profileLoaded: false,
    /** 请求代号防止登出后迟到的 profile 响应重新写入用户态。 */
    profileVersion: 0,
  }),
  actions: {
    /** 账密登录（含手机号作为账号，后端自动解析） */
    async login(username: string, password: string) {
      const pair = await apiLogin(username, password)
      saveTokens(pair)
      try {
        await this.loadProfile()
      } catch (e) {
        // 2026-09-12 修复（外部复核 P2）：profile 拉取失败时回滚令牌，
        // 避免"登录失败提示 + 刷新后仍登录"的矛盾状态
        clearTokens()
        throw e
      }
    },
    /** GitHub OAuth 回调：用一次性授权码换取令牌。 */
    async exchangeOAuthCode(code: string) {
      const pair = await apiExchangeOAuthCode(code)
      saveTokens(pair)
      await this.loadProfile()
    },
    /** 首屏入口共用：已有结果直接返回，否则等待正在进行的 profile 请求。 */
    async ensureProfile() {
      if (this.profileLoaded && this.user) return
      const pending = profileLoads.get(this)
      if (pending) return pending
      const request = this.loadProfile().finally(() => {
        profileLoads.delete(this)
      })
      profileLoads.set(this, request)
      return request
    },
    async loadProfile() {
      const version = ++this.profileVersion
      const profile = await fetchProfile()
      // 登出会使版本号失效，迟到的响应不得恢复已退出账号。
      if (version !== this.profileVersion) return
      this.user = profile
      this.profileLoaded = true
    },
    async logout() {
      // Cookie 模式也必须请求服务端登出，由后端吊销 refresh token 并清 Cookie。
      const refresh = getRefreshToken()
      try {
        await apiLogout(refresh)
      } finally {
        this.profileVersion += 1
        clearTokens()
        this.user = null
        this.profileLoaded = false
      }
    },
  },
})
