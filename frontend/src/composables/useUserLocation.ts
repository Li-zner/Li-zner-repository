/** 用户定位组合式函数：缓存 → geolocation → regeo → IP 定位，三级兜底（行为等价自 map.js）
 *
 * 2026-09-12 外部复核 P1：定位缓存按账号隔离（owner 进键），缓存含城市等隐私。
 */
import { ref } from 'vue'
import { ipLocate, reverseGeocode } from '../api/map'
import { StorageKey } from '../enums'
import type { UserLocation } from '../types'

const userLocation = ref<UserLocation | null>(null)
let boundOwner = ''

function locKey(owner = ''): string {
  return `${StorageKey.MapUserLocation}:${owner}`
}

/** 同步当前账号；账号变化时清掉内存定位，避免短暂展示上一账号的数据。 */
function bindOwner(owner: string): boolean {
  if (owner !== boundOwner) {
    boundOwner = owner
    userLocation.value = null
  }
  return Boolean(owner)
}

function readCache(owner = ''): UserLocation | null {
  if (!owner) return null
  try {
    const raw = localStorage.getItem(locKey(owner))
    if (!raw) return null
    const loc = JSON.parse(raw) as UserLocation
    // 历史垃圾定位（私有 IP 的"局域网"占位）视为未定位
    if (loc.city === '局域网') {
      localStorage.removeItem(locKey(owner))
      return null
    }
    return loc
  } catch {
    return null
  }
}

function persist(loc: UserLocation, owner = ''): void {
  if (!owner) return
  try {
    localStorage.setItem(locKey(owner), JSON.stringify(loc))
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
      return {
        lat: null,
        lng: null,
        city: d.city,
        province: d.province || d.city,
      }
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
export function useUserLocation(getOwner: () => string) {
  const ownerNow = () => {
    const owner = getOwner()
    return bindOwner(owner) ? owner : ''
  }

  async function locate(showPrompt = false): Promise<UserLocation | null> {
    const owner = ownerNow()
    if (!owner) return null
    // 1. 本地缓存（含垃圾定位清理）
    const cached = readCache(owner)
    if (cached) {
      if (ownerNow() !== owner) return null
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
          if (ownerNow() !== owner) return null
          persist(loc, owner)
          userLocation.value = loc
          return loc
        }
      } catch {
        /* regeo 失败 → IP 兜底 */
      }
    } else if (showPrompt) {
      alert('请允许浏览器获取位置信息，或在地图页手动选择出发地。')
    }
    // 3. IP 定位兜底
    const loc = await locateByIp()
    if (!loc || ownerNow() !== owner) return null
    persist(loc, owner)
    userLocation.value = loc
    return loc
  }

  /** 对外暴露的 IP 定位入口也必须读取当前账号，不能退回共享缓存。 */
  async function locateByIpCurrent(): Promise<UserLocation | null> {
    const owner = ownerNow()
    if (!owner) return null
    const loc = await locateByIp()
    if (!loc || ownerNow() !== owner) return null
    persist(loc, owner)
    userLocation.value = loc
    return loc
  }

  function clearLocation(): void {
    const owner = ownerNow()
    if (owner) localStorage.removeItem(locKey(owner))
    userLocation.value = null
  }

  return { userLocation, locate, locateByIp: locateByIpCurrent, clearLocation }
}
