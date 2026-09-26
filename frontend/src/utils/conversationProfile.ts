/** 会话画像：字段归一化、单轮提取、真实天气快照与历史摘要。 */

export interface ConversationWeatherDay {
  date: string
  day_weather: string
  night_weather: string
  day_temp: string
  night_temp: string
  day_wind: string
  night_wind: string
}

export interface ConversationProfile {
  name: string
  travelers: string
  origin: string
  destination: string
  budget: string
  days: string
  notes: string
  weather_city: string
  weather_updated_at: string
  weather_forecast: ConversationWeatherDay[]
  message_count?: number
}

export const USER_PREVIEW_CHARS = 100
export const COMPRESS_AFTER_USER_TURNS = 15

const TEXT_FIELDS = [
  'name', 'travelers', 'origin', 'destination', 'budget', 'days', 'notes',
  'weather_city', 'weather_updated_at',
] as const

const TRAVEL_CONTEXT_RE = /旅游|旅行|游玩|度假|自由行|攻略|推荐|规划|行程|去|到|前往|出发/u
const CN_DIGITS: Record<string, string> = {
  零: '0', 一: '1', 二: '2', 两: '2', 三: '3', 四: '4',
  五: '5', 六: '6', 七: '7', 八: '8', 九: '9',
}
const COUNT_PATTERN = '[0-9零一二两三四五六七八九十百千万]+'

export function emptyConversationProfile(): ConversationProfile {
  return {
    name: '', travelers: '', origin: '', destination: '', budget: '', days: '', notes: '',
    weather_city: '', weather_updated_at: '', weather_forecast: [],
  }
}

export function normalizeConversationProfile(raw: unknown): ConversationProfile {
  const data = raw && typeof raw === 'object' ? raw as Record<string, unknown> : {}
  const profile = emptyConversationProfile()
  for (const field of TEXT_FIELDS) profile[field] = String(data[field] ?? '').slice(0, 1000)
  if (Array.isArray(data.weather_forecast)) {
    profile.weather_forecast = data.weather_forecast
      .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object'))
      .slice(0, 3)
      .map((item) => ({
        date: String(item.date ?? '').slice(0, 16),
        day_weather: String(item.day_weather ?? '').slice(0, 32),
        night_weather: String(item.night_weather ?? '').slice(0, 32),
        day_temp: String(item.day_temp ?? '').slice(0, 16),
        night_temp: String(item.night_temp ?? '').slice(0, 16),
        day_wind: String(item.day_wind ?? '').slice(0, 32),
        night_wind: String(item.night_wind ?? '').slice(0, 32),
      }))
  }
  if (typeof data.message_count === 'number') profile.message_count = data.message_count
  return profile
}

export function mergeConversationProfile(
  current: ConversationProfile | undefined,
  updates: Partial<ConversationProfile>,
): ConversationProfile {
  const merged = normalizeConversationProfile(current)
  for (const [key, value] of Object.entries(updates)) {
    if (value === undefined || value === null || value === '') continue
    if (key === 'weather_forecast' && Array.isArray(value)) {
      merged.weather_forecast = normalizeConversationProfile({ weather_forecast: value }).weather_forecast
    } else if (key in merged) {
      ;(merged as unknown as Record<string, unknown>)[key] = value
    }
  }
  return merged
}

/** 把常见中文人数归一成阿拉伯数字，未知写法保持原样。 */
function normalizeCount(value: string): string {
  if (/^\d+$/u.test(value)) return value
  return CN_DIGITS[value] ?? value
}

/** 从单轮提问提取可确定字段；短地点回复仅在待补出发地时写入。 */
export function extractProfileUpdates(
  query: string,
  current?: ConversationProfile,
  pendingSlot?: 'origin' | 'destination',
): Partial<ConversationProfile> {
  const text = query.trim()
  const updates: Partial<ConversationProfile> = {}
  const name = text.match(/(?:我叫|叫我|姓名\s*[:：]?|我是)\s*([\u4e00-\u9fa5A-Za-z]{1,20})/u)
  if (name) updates.name = name[1]
  const people = text.match(
    new RegExp(
      `(${COUNT_PATTERN})\\s*个?人|一家\\s*(${COUNT_PATTERN})\\s*口`
      + `|(${COUNT_PATTERN})大\\s*(${COUNT_PATTERN})小`,
      'u',
    ),
  )
  if (people) {
    if (people[0].startsWith('一家')) {
      updates.travelers = `一家${normalizeCount(people[2])}口`
    } else if (people[3]) {
      updates.travelers = `${normalizeCount(people[3])}大${normalizeCount(people[4])}小`
    } else {
      updates.travelers = `${normalizeCount(people[1])}人`
    }
  }
  const explicitOrigin = text.match(
    /出发地(?:改成|改为|调整为|变成|换成|设为|是|为|到)?\s*([\u4e00-\u9fa5]{2,8}?)(?=出发|旅游|旅行|游玩|玩|[,，。；;\s]|$)/u,
  )
  if (explicitOrigin) updates.origin = explicitOrigin[1].replace(/市$/u, '')
  const origin = text.match(/(?:从|我在)\s*([\u4e00-\u9fa5]{2,8}?)(?:出发|到|去|$)/u)
  if (!updates.origin && origin) updates.origin = origin[1].replace(/市$/u, '')
  const explicitDestination = text.match(
    /目的地(?:改成|改为|调整为|变成|换成|设为|是|为|到)?\s*([\u4e00-\u9fa5]{2,8}?)(?=旅游|旅行|游玩|玩|[,，。；;\s]|$)/u,
  )
  if (explicitDestination) updates.destination = explicitDestination[1].replace(/市$/u, '')
  const destination = text.match(/(?:去|到|前往|飞)\s*([\u4e00-\u9fa5]{2,8}?)(?:旅游|旅行|玩|游玩|$)/u)
    ?? text.match(/([\u4e00-\u9fa5]{2,6}?)(?:市)?(?:旅游|旅行|游玩)/u)
  if (!updates.destination && destination && TRAVEL_CONTEXT_RE.test(text)) {
    updates.destination = destination[1].replace(/市$/u, '')
  }
  const days = text.match(/(\d+)\s*天/u)
  if (days) updates.days = `${days[1]}天`
  const budget = text.match(
    new RegExp(
      `预算(?:是|为|大约|大概|控制在|在|改成|改为|调整为|变成|调成|设为|设置为)?\\s*`
      + `(${COUNT_PATTERN}(?:元|块|万)?)`,
      'u',
    ),
  )
  if (budget) updates.budget = budget[1]
  const barePlace = /^[\u4e00-\u9fa5]{2,8}$/u.test(text)
  if (!updates.origin && !current?.origin && current?.destination && barePlace) {
    updates.origin = text.replace(/市$/u, '')
  }
  if (pendingSlot === 'origin' && !updates.origin && barePlace) {
    updates.origin = text.replace(/市$/u, '')
  }
  if (pendingSlot === 'destination' && !updates.destination && barePlace) {
    updates.destination = text.replace(/市$/u, '')
  }
  return updates
}

export function weatherSnapshotFromTool(result: unknown): Partial<ConversationProfile> {
  if (!result || typeof result !== 'object') return {}
  const data = result as Record<string, unknown>
  if (data.error || !Array.isArray(data.forecast)) return {}
  const forecast = normalizeConversationProfile({ weather_forecast: data.forecast }).weather_forecast
  if (!forecast.length) return {}
  return {
    weather_city: String(data.city ?? ''),
    weather_updated_at: new Date().toISOString(),
    weather_forecast: forecast,
  }
}

export function userPreview(content: string, max = USER_PREVIEW_CHARS): string {
  const text = content.trim()
  return text.length > max ? `${text.slice(0, max)}…` : text
}

/** 生成历史栏摘要标题：优先使用已确认槽位，避免直接截取用户原话。 */
export function conversationTitle(
  query: string,
  profile?: ConversationProfile,
): string {
  const text = query.trim().replace(/\s+/gu, ' ')
  const destination = profile?.destination?.trim()
  if (destination) {
    const days = (profile?.days ?? '').trim()
    const trip = days ? `${days.replace(/天/u, '日')}游` : '旅行'
    const meta = [profile?.travelers, profile?.budget].filter(Boolean).join(' · ')
    return truncateTitle(`${destination}${trip}规划${meta ? ` · ${meta}` : ''}`)
  }
  const cleaned = text
    .replace(/^(?:请|帮我|我想|我要|麻烦|可以)\s*/u, '')
    .replace(/[，。！？!?；;]/gu, ' ')
    .replace(/\s+/gu, ' ')
    .trim()
  return truncateTitle(cleaned || text)
}

function truncateTitle(title: string, max = 30): string {
  return title.length > max ? `${title.slice(0, max)}…` : title
}

export function needsContextCompression(userTurns: number): boolean {
  return userTurns > COMPRESS_AFTER_USER_TURNS
}
