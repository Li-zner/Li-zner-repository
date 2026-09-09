/** ============================================================
 *  可复用资产：sse-framer.ts
 *  来源：agent_gateway/frontend/src/api/sse.ts
 *  实战验证：生产运行中 + 6 条单测（sse-framer.test.ts）
 *  依赖：零依赖
 *  提取：2026-09-06；二次复用后请在来源行补注项目名
 *  ============================================================ */
/**
 * SSE 流式解析器（与传输解耦，便于单测）
 *
 * 后端 SSE 协议：每行 `data: {"type": ..., "content": ...}`，终止 `data: [DONE]`。
 * 解析器按行切分，跨 chunk 的半行缓存在 buffer 中，直到出现换行才解析。
 */
export interface SseEvent {
  type: string
  content?: unknown
  [key: string]: unknown
}

export type SseLineSink = (event: SseEvent | null, rawLine: string) => void

/** 处理一行 SSE：data: [JSON] → 事件；data: [DONE] → null 事件；其余行忽略 */
export function parseSseLine(line: string): SseEvent | null {
  if (!line.startsWith('data:')) return null
  const payload = line.slice(5).trim()
  if (!payload || payload === '[DONE]') return null
  try {
    return JSON.parse(payload) as SseEvent
  } catch {
    return null
  }
}

/**
 * 流式分帧器：feed() 喂入任意切分的文本块，内部按换行攒帧并回调完整行
 */
export class SseLineFramer {
  private buffer = ''

  constructor(private onLine: SseLineSink) {}

  feed(text: string): void {
    this.buffer += text
    let idx: number
    while ((idx = this.buffer.indexOf('\n')) >= 0) {
      const line = this.buffer.slice(0, idx).replace(/\r$/, '')
      this.buffer = this.buffer.slice(idx + 1)
      if (line.trim()) this.onLine(parseSseLine(line), line)
    }
  }

  /** 流结束时冲刷残余缓冲（协议上不应有，防御性处理） */
  flush(): void {
    if (this.buffer.trim()) this.onLine(parseSseLine(this.buffer), this.buffer)
    this.buffer = ''
  }
}
