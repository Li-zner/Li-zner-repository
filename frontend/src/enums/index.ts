/** 枚举与常量（对齐成熟工程 enums/ 惯例） */

/** 后端 SSE 事件类型（POST /v2/chat/stream） */
export enum SseEventType {
  Thought = 'thought',
  ReasoningChunk = 'reasoning_chunk',
  ReasoningDone = 'reasoning_done',
  AnswerChunk = 'answer_chunk',
  AnswerComplete = 'answer_complete',
  ToolCall = 'tool_call',
  ToolResult = 'tool_result',
}

/** localStorage 键（集中定义防拼写漂移） */
export enum StorageKey {
  AccessToken = 'gw_access_token',
  RefreshToken = 'gw_refresh_token',
  Locale = 'travel_lang',
  MapHistory = 'map_history',
  MapFavorites = 'map_favorites',
  MapUserLocation = 'map_user_location',
  AutoPrompt = 'travel_auto_prompt',
  Theme = 'gw_theme',
}

/** 可用主题（settings 弹窗三选一） */
export enum Theme {
  Classic = 'classic',
  Glass = 'glass',
  Dark = 'dark',
}
