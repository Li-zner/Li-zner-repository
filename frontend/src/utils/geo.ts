/** 地图 GeoJSON 纯工具（自 map.js 抽出，可单测） */
import type { GeoFeature, GeoJson } from '../types'

const DATAV_BASE = 'https://geo.datav.aliyun.com/areas_v3/bound'

/** 省级全国 GeoJSON 地址 */
export const PROVINCE_GEO_URL = `${DATAV_BASE}/100000_full.json`

/** 城市级 GeoJSON 地址（adcode 由调用方保证为数字串） */
export function buildCityGeoUrl(adcode: string | number): string {
  const value = String(adcode)
  if (!/^\d{6}$/.test(value)) throw new Error('adcode 必须为 6 位数字')
  return `${DATAV_BASE}/${value}_full.json`
}

/** 显示名：去掉"市/省"后缀（保留自治州全称，如"海西蒙古族藏族自治州"） */
export function displayName(name: string): string {
  return (name || '').replace(/(市|省)$/, '')
}

/**
 * 计算 GeoJSON 各区域标签锚点：centroid（几何质心）优先 → center（行政中心）→ 中国中心兜底
 */
export function computeCenters(features: GeoFeature[]): { name: string; value: [number, number] }[] {
  return features.map((f) => {
    const c = f.properties.centroid ?? f.properties.center
    if (c && c.length === 2) return { name: f.properties.name, value: c }
    return { name: f.properties.name, value: [105, 35] }
  })
}

/** 下钻时的初始缩放：城市越少放大越高（阈值自 map.js 行为等价平移） */
export function initialZoomFor(featureCount: number): number {
  if (featureCount <= 5) return 2.0
  if (featureCount <= 10) return 1.6
  if (featureCount <= 20) return 1.3
  return 1.1
}

/** 从 GeoJSON 中按名称找 adcode（id 或 properties.adcode） */
export function findAdcode(geo: GeoJson, name: string): string | number | null {
  const f = geo.features.find((f) => f.properties.name === name)
  if (!f) return null
  const candidate = f.id ?? f.properties.adcode
  return /^\d{6}$/.test(String(candidate ?? '')) ? candidate ?? null : null
}
