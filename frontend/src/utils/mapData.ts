/**
 * 地图页数据与本地存储助手（2026-09-12 自 MapView.vue 拆出，满足 600 行门禁）。
 * CITY_LIST：快捷直达城市；MUNI：直辖市/特区 adcode（下钻行为不同）。
 */
import { StorageKey } from '../enums'

// 快捷城市 adcode：直辖市/特区走 MUNI（省级码，下钻行为不同）；其余必须是**市级**码。
// 曾 12/16 条填成省级码（如深圳=广州同为 440000），下钻渲染整省、标题退化成
// features[0] 的市名。逐条以 DataV `<code>_full.json` 返回该辖区列表为准校验：
// 440300→罗湖/福田/南山…、460200→海棠/吉阳/天涯/崖州、350200→思明/海沧…、
// 230100→道里/南岗…（省码 440000/460000/350000/230000 返回的是整省地市）。
export const CITY_LIST: { city: string; adcode: number }[] = [
  { city: '北京', adcode: 110000 }, { city: '天津', adcode: 120000 },
  { city: '上海', adcode: 310000 }, { city: '重庆', adcode: 500000 },
  { city: '广州', adcode: 440100 }, { city: '深圳', adcode: 440300 },
  { city: '杭州', adcode: 330100 }, { city: '南京', adcode: 320100 },
  { city: '成都', adcode: 510100 }, { city: '武汉', adcode: 420100 },
  { city: '长沙', adcode: 430100 }, { city: '西安', adcode: 610100 },
  { city: '厦门', adcode: 350200 }, { city: '昆明', adcode: 530100 },
  { city: '哈尔滨', adcode: 230100 }, { city: '三亚', adcode: 460200 },
]

export const MUNI = [110000, 120000, 310000, 500000, 810000, 820000, 710000]

/** 读字符串列表：损坏/非数组数据兜底为空（否则 setup 抛异常整页白屏）。
 *  owner：账号维度（2026-09-12 外部复核 P1——地图历史/收藏原为全局键，跨账号可读） */
export function loadStringList(key: StorageKey, owner = ''): string[] {
  if (!owner) return []
  try {
    const namespaced = `${key}:${owner}`
    const parsed = JSON.parse(localStorage.getItem(namespaced) ?? '[]')
    return Array.isArray(parsed) ? parsed : []
  } catch {
    return []
  }
}

export function saveStringList(key: StorageKey, list: string[], owner = ''): void {
  if (!owner) return
  const namespaced = `${key}:${owner}`
  localStorage.setItem(namespaced, JSON.stringify(list))
}
