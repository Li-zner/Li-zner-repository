/** 用户定位组合式函数：缓存 → geolocation → regeo → IP 定位，三级兜底（行为等价自 map.js） */
import { ref } from 'vue'
import { ipLocate, reverseGeocode } from '../api/map'
import { StorageKey } from '../enums'
import type { UserLocation } from '../types'

const userLocation = ref<UserLocation | null>(null)

function readCache(): UserLocation | null {
  try {
    const raw = localStorage.getItem(StorageKey.MapUserLocation)
    if (!raw) return null
    const loc = JSON.parse(raw) as UserLocation
    // 历史垃圾定位（私有 IP 的"局域网"占位）视为未定位
    if (loc.city === '局域网') {
      localStorage.removeItem(StorageKey.MapUserLocation)
      return null
    }
    return loc
  } catch {
    return null
  }
}

function persist(loc: UserLocation): void {
  userLocation.value = loc
  try {
    localStorage.setItem(StorageKey.MapUserLocation, JSON.stringify(loc))
  } catch {
    /* 隐私模式等场景静默 */
  }
}

function geolocate(timeout = 8000): Promise<GeolocationPosition | null> {
  return new Promise((resolve) => {
    if (!navigator.geolocation) return resolve(null)
    navigator.geolocation.getCurrentPosition(resolve, () => resolve(null), {
      timeout,
      enableHighAccuracy: false,
    })
  })
}

async function locateByIp(): Promise<UserLocation | null> {
  try {
    const d = await ipLocate()
    // 后端已对私有 IP 返回空，这里再滤一次"局域网"占位（双保险）
    if (d.city && d.city !== '局域网') {
      const loc: UserLocation = {
        lat: null,
        lng: null,
        city: d.city,
        province: d.province || d.city,
      }
      persist(loc)
      return loc
    }
  } catch {
    /* IP 定位失败不阻塞 */
  }
  return null
}

/**
 * 请求用户位置（统一入口）。
 * @param showPrompt 定位被拒绝时是否弹提示（引导去浏览器设置开启权限）
 */
export function useUserLocation() {
  async function locate(showPrompt = false): Promise<UserLocation | null> {
    // 1. 本地缓存（含垃圾定位清理）
    const cached = readCache()
    if (cached) {
      userLocation.value = cached
      return cached
    }
    // 2. 浏览器定位（拒绝权限则提示 + IP 兜底；失败/超时 → IP 兜底）
    const pos = await geolocate()
    if (pos) {
      try {
        const data = await reverseGeocode(pos.coords.latitude, pos.coords.longitude)
        if (data.city) {
          const loc: UserLocation = {
            lat: pos.coords.latitude,
            lng: pos.coords.longitude,
            city: data.city,
            province: data.province || data.city,
          }
          persist(loc)
          return loc
        }
      } catch {
        /* regeo 失败 → IP 兜底 */
      }
    } else if (showPrompt) {
      alert('请允许浏览器获取位置信息，或在地图页手动选择出发地。')
    }
    // 3. IP 定位兜底
    return locateByIp()
  }

  function clearLocation(): void {
    localStorage.removeItem(StorageKey.MapUserLocation)
    userLocation.value = null
  }

  return { userLocation, locate, locateByIp, clearLocation }
}
