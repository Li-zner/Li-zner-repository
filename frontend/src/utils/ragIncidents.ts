/** incident 分组/排序纯逻辑，从 RagConsoleView 抽出以压减主视图行数。 */
import type { RagIncident } from '../api/ragAdmin'

const SEVERITY_RANK: Record<string, number> = {
  critical: 0,
  high: 1,
  medium: 2,
  low: 3,
}

/** 判断事件是否仍需要运营处理。 */
export function isHandledIncident(item: RagIncident): boolean {
  return ['resolved', 'closed'].includes(item.status)
}

/** 解析 ISO 时间，失败时回落到 0，保证比较函数稳定。 */
export function parseTime(value?: string | null): number {
  if (!value) return 0
  const parsed = Date.parse(value)
  return Number.isNaN(parsed) ? 0 : parsed
}

/** 事件最近出现时间。 */
export function incidentTime(item: RagIncident): number {
  return parseTime(item.last_seen_at)
}

/** 已处理事件的处理时间，取解决、关闭、最近出现时间的最大值。 */
export function handledTime(item: RagIncident): number {
  return Math.max(parseTime(item.resolved_at), parseTime(item.closed_at), incidentTime(item))
}

/** 未处理事件先按严重度，再按最近出现时间排序。 */
export function compareUnhandledIncidents(a: RagIncident, b: RagIncident): number {
  const severityDiff = (SEVERITY_RANK[a.severity] ?? 99) - (SEVERITY_RANK[b.severity] ?? 99)
  return severityDiff || incidentTime(b) - incidentTime(a)
}

/** 已处理事件按最近处理时间倒序，最新处理的排分组头部。 */
export function compareHandledIncidents(a: RagIncident, b: RagIncident): number {
  return handledTime(b) - handledTime(a)
}

/** 未处理且静默超过 staleHours 的事件，与服务端批量清理口径一致（仅用于前端预告数量）。 */
export function staleClearableIncidents(
  items: RagIncident[],
  staleHours: number,
  now = Date.now(),
): RagIncident[] {
  const thresholdMs = staleHours * 3600_000
  return items.filter(
    (item) => !isHandledIncident(item) && now - incidentTime(item) >= thresholdMs,
  )
}

/** 批量清理的确认文案：预览前 5 条事件，口径与服务端一致。 */
export function staleClearConfirmMessage(items: RagIncident[], staleHours: number): string {
  const preview = items.slice(0, 5)
    .map((item) => `#${item.incident_id} ${item.code}`)
    .join('、')
  const more = items.length > 5 ? ` 等 ${items.length} 条` : ''
  return `以下 ${items.length} 条超过 ${staleHours} 小时未复发的活跃事件将被标记为已解决`
    + `（日志保留、不删除；之后若再触发会自动开新事件）：\n${preview}${more}\n\n确认清理？`
}
