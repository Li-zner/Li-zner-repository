/** RAG 中控台展示格式化：时长人话化、相对时间与负责人脱敏。 */

/** 毫秒转人话时长：34227 → "34.2秒"；表格内的紧凑值仍用 ms。 */
export function fmtDuration(ms?: number | null): string {
  if (ms == null) return '-'
  if (ms < 1000) return `${Math.round(ms)}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}秒`
  if (ms < 3600000) return `${(ms / 60000).toFixed(1)}分`
  return `${(ms / 3600000).toFixed(1)}小时`
}

/** 「距今」标签（秒）：低流量时段请求间隔本来就大，只陈述事实不做告警色。 */
export function fmtAgo(seconds?: number | null): string {
  if (seconds == null || seconds < 0) return '-'
  if (seconds < 90) return `${Math.round(seconds)} 秒前`
  if (seconds < 5400) return `${Math.round(seconds / 60)} 分钟前`
  if (seconds < 144000) return `${(seconds / 3600).toFixed(1)} 小时前`
  return `${(seconds / 86400).toFixed(1)} 天前`
}

/** 负责人脱敏：本仓用户名即手机号，编码规范要求响应/界面不明文展示。 */
export function maskOwner(value?: string | null): string {
  if (!value) return '-'
  return /^1\d{10}$/.test(value) ? `${value.slice(0, 3)}****${value.slice(7)}` : value
}
