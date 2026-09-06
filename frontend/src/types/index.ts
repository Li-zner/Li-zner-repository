/** 全局共享类型（跨模块复用的领域模型） */

/** 用户地理位置（lat/lng 可能为空：IP 定位兜底只有城市） */
export interface UserLocation {
  province: string
  city: string
  lat: number | null
  lng: number | null
}

/** GeoJSON Feature（DataV 行政区划， properties 按需声明） */
export interface GeoFeature {
  type: 'Feature'
  properties: {
    name: string
    adcode?: string | number
    centroid?: [number, number]
    center?: [number, number]
    [key: string]: unknown
  }
  id?: string | number
  geometry: unknown
}

export interface GeoJson {
  type: 'FeatureCollection'
  features: GeoFeature[]
}
