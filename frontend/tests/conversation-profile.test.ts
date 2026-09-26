/** 会话画像提取、天气快照与15轮压缩边界。 */
import { describe, expect, it } from 'vitest'
import {
  conversationTitle,
  extractProfileUpdates,
  mergeConversationProfile,
  needsContextCompression,
  weatherSnapshotFromTool,
} from '../src/utils/conversationProfile'

describe('conversation profile', () => {
  it('从一句话中提取姓名、人数、出发地、目的地、预算和天数', () => {
    const updates = extractProfileUpdates(
      '我叫南，我们4个人，从广州出发，去成都旅游5天，预算8000元',
    )
    expect(updates).toMatchObject({
      name: '南',
      travelers: '4人',
      origin: '广州',
      destination: '成都',
      days: '5天',
      budget: '8000元',
    })
  })

  it('短回复梧州时按已有目的地补出发地', () => {
    const current = mergeConversationProfile(undefined, { destination: '渭南' })
    const updates = extractProfileUpdates('梧州', current)
    expect(updates.origin).toBe('梧州')
    expect(updates.destination).toBeUndefined()
  })

  it('用户改口时更新中文人数和预算', () => {
    expect(extractProfileUpdates('预算改成5000元，还是两个人去')).toMatchObject({
      travelers: '2人',
      budget: '5000元',
    })
  })

  it('用户显式改出发地和目的地时覆盖旧地点', () => {
    expect(
      extractProfileUpdates('出发地改成南宁，目的地改成成都，4个人去5天'),
    ).toMatchObject({
      origin: '南宁',
      destination: '成都',
      travelers: '4人',
      days: '5天',
    })
  })

  it('历史标题使用目的地、天数和人数预算生成摘要', () => {
    const profile = mergeConversationProfile(undefined, {
      destination: '西安',
      days: '4天',
      travelers: '2人',
      budget: '6000元',
    })
    expect(conversationTitle('按新方案重新规划', profile)).toBe(
      '西安4日游规划 · 2人 · 6000元',
    )
  })

  it('首轮只有目的地时生成简洁旅行标题', () => {
    const profile = mergeConversationProfile(undefined, { destination: '渭南' })
    expect(conversationTitle('渭南旅游推荐，请先问出发地', profile)).toBe('渭南旅行规划')
  })

  it('只接收真实天气结果并限制三天', () => {
    const weather = weatherSnapshotFromTool({
      city: '渭南',
      forecast: [
        { date: '2026-09-21', day_weather: '晴', day_temp: '28' },
        { date: '2026-09-22', day_weather: '多云' },
        { date: '2026-09-23', day_weather: '小雨' },
        { date: '2026-09-24', day_weather: '阴' },
      ],
    })
    expect(weather.weather_city).toBe('渭南')
    expect(weather.weather_forecast).toHaveLength(3)
    expect(weather.weather_forecast?.[0]?.day_weather).toBe('晴')
  })

  it('第16次用户提问才触发上下文压缩标记', () => {
    expect(needsContextCompression(15)).toBe(false)
    expect(needsContextCompression(16)).toBe(true)
  })
})
