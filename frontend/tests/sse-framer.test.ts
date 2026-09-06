/** SSE 分帧器单测：跨 chunk 半行攒帧 / [DONE] 忽略 / 非 data 行忽略 */
import { describe, expect, it, vi } from 'vitest'
import { SseLineFramer, parseSseLine } from '../src/api/sse'

describe('parseSseLine', () => {
  it('解析 data: JSON 行', () => {
    expect(parseSseLine('data: {"type":"answer_chunk","content":"你好"}')).toEqual({
      type: 'answer_chunk',
      content: '你好',
    })
  })
  it('[DONE] 与空行返回 null', () => {
    expect(parseSseLine('data: [DONE]')).toBeNull()
    expect(parseSseLine('')).toBeNull()
  })
  it('非 data 行返回 null', () => {
    expect(parseSseLine(': keep-alive')).toBeNull()
  })
  it('坏 JSON 不抛异常返回 null', () => {
    expect(parseSseLine('data: {oops')).toBeNull()
  })
})

describe('SseLineFramer', () => {
  it('跨 chunk 半行攒帧后按行回调', () => {
    const onLine = vi.fn()
    const framer = new SseLineFramer(onLine)
    // 构造跨 chunk 切分：第一段止于 JSON 字符串内部（半行），第二段补完并换行
    const first = 'data: {"type":"a"'
    const second = ',"content":1}\ndata: {"type":"b"}\n'
    framer.feed(first)
    expect(onLine).not.toHaveBeenCalled() // 半行不回调
    framer.feed(second)
    expect(onLine).toHaveBeenCalledTimes(2)
    expect(onLine.mock.calls[0][0]).toEqual({ type: 'a', content: 1 })
    expect(onLine.mock.calls[1][0]).toEqual({ type: 'b' })
  })
  it('flush 冲刷无换行的残余行', () => {
    const onLine = vi.fn()
    const framer = new SseLineFramer(onLine)
    framer.feed('data: {"type":"end"}')
    framer.flush()
    expect(onLine).toHaveBeenCalledWith({ type: 'end' }, 'data: {"type":"end"}')
  })
})
