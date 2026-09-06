/** 地图相关 API（/api/map 系列，后端代理高德/DeepSeek） */
import { get, post } from './http'

export interface WeatherResponse {
  status: string
  lives?: { city: string; weather: string; temperature: string; winddirection: string; windpower: string }[]
}

export interface RecommendResponse {
  foods?: string[]
  spots?: string[]
}

export interface RegeoResponse {
  city: string
  province?: string
}

export interface IpLocResponse {
  city: string
  province?: string
}

export interface RouteLeg {
  from: string
  to: string
  mode: string
  duration_min: number
  distance_km: number
}

export interface AmapRouteResponse {
  departure_location?: string
  destination_location?: string
  [key: string]: unknown
}

export async function getWeather(city: string, adcode?: string | number): Promise<string> {
  const q = encodeURIComponent(String(adcode ?? city))
  const data = await get<WeatherResponse>(`/api/map/weather?city=${q}`)
  if (data.status === '1' && data.lives?.length) {
    const l = data.lives[0]
    return `${l.weather} ${l.temperature}℃ · ${l.winddirection}风 ${l.windpower}级`
  }
  throw new Error('天气获取失败')
}

export async function getRecommendations(city: string): Promise<{ foods: string[]; spots: string[] }> {
  const data = await post<RecommendResponse>(
    `/api/map/recommend?city=${encodeURIComponent(city)}`)
  if (!data || (!data.foods && !data.spots)) throw new Error('推荐数据格式错误')
  return { foods: data.foods ?? [], spots: data.spots ?? [] }
}

export async function reverseGeocode(lat: number, lng: number): Promise<RegeoResponse> {
  return get<RegeoResponse>(`/api/map/regeo?lat=${lat}&lng=${lng}`)
}

export async function ipLocate(): Promise<IpLocResponse> {
  return get<IpLocResponse>('/api/map/iploc')
}

export async function amapRoute(departure: string, destination: string): Promise<AmapRouteResponse> {
  return post<AmapRouteResponse>('/api/map/amap-route', { departure, destination })
}
