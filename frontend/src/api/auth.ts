/** 认证 / 用户信息 API（/api/login /api/logout /api/user/profile） */
import { get, post, saveTokens, type TokenPair } from './http'

export interface UserProfile {
  username: string
  phone: string
  role: string
  display_name: string
  email: string
  quota_limited: boolean
  used_requests: number
  remaining_questions: number
  created_at: string
}

export async function login(username: string, password: string): Promise<TokenPair> {
  const pair = await post<TokenPair>('/api/login', { username, password })
  saveTokens(pair)
  return pair
}

/** 登出：撤销 refresh token（后端 jti 进黑名单），并清本地态 */
export async function logout(refreshToken: string): Promise<void> {
  try {
    await post('/api/logout', undefined, {
      Authorization: `Bearer ${refreshToken}`,
    })
  } catch {
    /* 后端撤销失败不阻塞本地登出（token 自身到期失效） */
  }
}

export function fetchProfile(): Promise<UserProfile> {
  return get<UserProfile>('/api/user/profile')
}
