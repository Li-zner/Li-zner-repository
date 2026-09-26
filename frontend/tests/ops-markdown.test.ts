/** 运维助手小标题升级回归：独立【xx】段落必须渲染为 h4.ops-section。 */
import { describe, expect, it } from 'vitest'
import { renderOpsMarkdown } from '../src/utils/opsMarkdown'

describe('renderOpsMarkdown', () => {
  it('独立小标题升级为 h4，容忍尾随冒号', () => {
    const html = renderOpsMarkdown('【异常结论】\n\n- 条目一\n\n【建议动作】：\n\n- 条目二')
    expect(html).toContain('<h4 class="ops-section">异常结论</h4>')
    expect(html).toContain('<h4 class="ops-section">建议动作</h4>')
    expect(html).not.toContain('<p>【')
  })

  it('正文内嵌的【】不被误升级', () => {
    const html = renderOpsMarkdown('请执行 patch {"retrieval.vector.enabled":true}，关注【精度】变化')
    expect(html).not.toContain('ops-section')
  })
})
