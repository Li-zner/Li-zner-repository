/** 地图相关 API（/api/map 系列，后端代理高德/DeepSeek） */
import { get, post } from './http'

export interface WeatherDay {
  label: string   // 今天 / 明天 / 后天
  weather: string // 白天天气状况
  temp: string    // 夜温~日温，如 24~28℃
}

export interface WeatherResponse {
  status: string
  days?: WeatherDay[]
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
  if (data.status === '1' && data.days?.length) {
    const note = (data as { _note?: string })._note ?? ''
    // 日期写清楚：今天/明天/后天各占一行（气泡内换行渲染），白天天气+夜温~日温区间
    return note + data.days.map((d) => `${d.label}${d.weather} ${d.temp}`).join('\n')
  }
  throw new Error('该地区暂无天气数据')
}

export async function getRecommendations(city: string, force = false): Promise<{ foods: string[]; spots: string[] }> {
  const data = await post<RecommendResponse>(
    `/api/map/recommend?city=${encodeURIComponent(city)}${force ? '&force=1' : ''}`)
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
