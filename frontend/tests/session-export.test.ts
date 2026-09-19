// sessionToMarkdown 导出格式回归检查
import { describe, expect, it } from 'vitest'
import { sessionToMarkdown } from '../src/utils/sessions'

const session = {
  id: 's1',
  personaId: 'unified',
  title: '成都5日游',
  conversationId: 'c1',
  updatedAt: 0,
  messages: [
    { id: 'm1', role: 'user' as const, content: '帮我规划成都行程', timestamp: 0 },
    { id: 'm2', role: 'assistant' as const, content: '好的，以下是行程…', timestamp: 0 },
  ],
}

describe('sessionToMarkdown', () => {
  it('含标题、角色小节与消息正文', () => {
    const md = sessionToMarkdown(session, 'zh')
    expect(md).toContain('# 成都5日游')
    expect(md).toContain('## 用户')
    expect(md).toContain('帮我规划成都行程')
    expect(md).toContain('## 助手')
    expect(md).toContain('好的，以下是行程…')
  })

  it('含导出时间标注', () => {
    expect(sessionToMarkdown(session, 'zh')).toMatch(/> 导出于 /)
    expect(sessionToMarkdown(session, 'en')).toMatch(/> Exported at /)
  })
})
