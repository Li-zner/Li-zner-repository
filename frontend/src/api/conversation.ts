/** 对话管理 API：删除服务端对话记忆与画像。 */
import { request } from './http'

export function deleteConversation(conversationId: string): Promise<void> {
  return request<void>(
    `/api/conversations/${encodeURIComponent(conversationId)}`,
    { method: 'DELETE' },
  )
}
