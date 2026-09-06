/** 用户域杂项 API：修改密码 / 消息评分 */
import { post } from './http'

export function changePassword(oldPassword: string, newPassword: string): Promise<{ message: string }> {
  return post('/api/user/change-password', { old_password: oldPassword, new_password: newPassword })
}

export interface RateResult {
  message: string
  rating: number
  already_rated: boolean
}

export function rateMessage(payload: {
  rating: number
  session_id: string
  user_message: string
  assistant_message: string
}): Promise<RateResult> {
  return post('/api/message/rate', payload)
}
