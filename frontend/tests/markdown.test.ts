/** markdown 渲染单测：正常渲染 + XSS 消毒（LLM 输出防注入） */
import { describe, expect, it } from 'vitest'
import { renderMarkdown } from '../src/utils/markdown'

describe('renderMarkdown', () => {
  it('渲染加粗与代码', () => {
    const html = renderMarkdown('**hi** `code`')
    expect(html).toContain('<strong>hi</strong>')
    expect(html).toContain('<code>code</code>')
  })
  it('脚本标签必须被消毒掉', () => {
    const html = renderMarkdown('<script>alert(1)</script>**ok**')
    expect(html).not.toContain('<script')
    expect(html).toContain('ok')
  })
  it('onerror 内联事件被消毒掉', () => {
    const html = renderMarkdown('<img src=x onerror="alert(1)">')
    expect(html).not.toContain('onerror')
  })
  it('空文本返回空串', () => {
    expect(renderMarkdown('')).toBe('')
  })
})
