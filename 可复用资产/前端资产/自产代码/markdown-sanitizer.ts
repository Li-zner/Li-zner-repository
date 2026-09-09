/** ============================================================
 *  可复用资产：markdown-sanitizer.ts
 *  来源：agent_gateway/frontend/src/utils/markdown.ts
 *  实战验证：生产运行中 + 消毒回归单测
 *  依赖：npm i marked dompurify
 *  提取：2026-09-06；二次复用后请在来源行补注项目名
 *  ============================================================ */
/** Markdown 渲染：marked 解析 + DOMPurify 消毒（防 LLM 输出注入脚本） */
import { marked } from 'marked'
import DOMPurify from 'dompurify'

marked.setOptions({ breaks: true, gfm: true })

export function renderMarkdown(text: string): string {
  if (!text) return ''
  const html = marked.parse(text, { async: false }) as string
  return DOMPurify.sanitize(html)
}
