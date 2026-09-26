/** 运维助手回复专用渲染：格式契约里的独立【小标题】行升级为真标题。
 *  renderMarkdown 已做 HTML 转义，这里只匹配纯文本段落，无注入面。 */
import { renderMarkdown } from './markdown'

export function renderOpsMarkdown(content: string): string {
  return renderMarkdown(content).replace(
    /<p>\s*【([^】]{2,12})】[：:]?\s*<\/p>/g,
    '<h4 class="ops-section">$1</h4>',
  )
}
