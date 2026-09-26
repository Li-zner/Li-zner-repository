/** 对话管理 API：会话画像读取/自动同步与删除服务端对话记忆。 */
import { request } from './http'
import type { ConversationProfile } from '../utils/conversationProfile'

export interface ConversationProfileResponse {
  conversation_id: string
  profile: ConversationProfile
}

export function getConversationProfile(
  conversationId: string,
): Promise<ConversationProfileResponse> {
  return request<ConversationProfileResponse>(
    `/api/conversations/${encodeURIComponent(conversationId)}/profile`,
  )
}

export function patchConversationProfile(
  conversationId: string,
  updates: Partial<ConversationProfile>,
): Promise<ConversationProfileResponse> {
  return request<ConversationProfileResponse>(
    `/api/conversations/${encodeURIComponent(conversationId)}/profile`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(updates),
    },
  )
}

export function deleteConversation(conversationId: string): Promise<void> {
  return request<void>(
    `/api/conversations/${encodeURIComponent(conversationId)}`,
    { method: 'DELETE' },
  )
}
