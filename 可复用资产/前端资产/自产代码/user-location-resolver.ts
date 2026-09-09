/** ============================================================
 *  可复用资产：user-location-resolver.ts
 *  来源：agent_gateway/frontend/src/composables/useUserLocation.ts
 *  实战验证：生产运行中
 *  依赖：注入式定位源（原版接高德 Web 服务：ipLocate / reverseGeocode，密钥走 .env）
 *  提取：2026-09-06；二次复用后请在来源行补注项目名
 *  ============================================================ */

/** 用户位置：lng/lat 仅浏览器精确定位成功时才有（IP 定位只有城市粒度） */
export interface UserLocation {
  lat: number | null
  lng: number | null
  city: string
  province: string
}

/** 外部定位源接口：接入高德/其他服务商时实现这两个函数（密钥走 .env，勿硬编码） */
export interface LocationSource {
  /** IP 定位 → 城市级（不可用或局域网返回 null） */
  ipLocate: () => Promise<{ city: string; province?: string } | null>
  /** 经纬度逆地编 → 城市/省份（失败抛异常或返回空 city） */
  reverseGeocode: (lat: number, lng: number) => Promise<{ city: string; province?: string }>
}

/** 缓存键：多应用共存的 localStorage 请换带前缀的键名 */
export const LOCATION_CACHE_KEY = 'map_user_location'

/** 读本地缓存：损坏数据与历史垃圾定位（如"局域网"占位）一律视为未定位 */
function readCache(): UserLocation | null {
  try {
    const raw = localStorage.getItem(LOCATION_CACHE_KEY)
    if (!raw) return null
    const loc = JSON.parse(raw) as UserLocation
    if (!loc.city || loc.city === '局域网') {
      localStorage.removeItem(LOCATION_CACHE_KEY)
      return null
    }
    return loc
  } catch {
    return null
  }
}

function persist(loc: UserLocation): void {
  try {
    localStorage.setItem(LOCATION_CACHE_KEY, JSON.stringify(loc))
  } catch {
    /* 隐私模式等场景静默 */
  }
}

/** 浏览器 geolocation：超时/拒绝/不支持一律 resolve null（调用方走兜底） */
function geolocate(timeout = 8000): Promise<GeolocationPosition | null> {
  return new Promise((resolve) => {
    if (!navigator.geolocation) return resolve(null)
    navigator.geolocation.getCurrentPosition(resolve, () => resolve(null), {
      timeout,
      enableHighAccuracy: false,
    })
  })
}

export interface UserLocationResolver {
  /** 当前位置（响应式由使用方按需包 ref） */
  userLocation: UserLocation | null
  /**
   * 统一定位入口：缓存 → 浏览器定位+逆地编 → IP 兜底。
   * @param showPrompt 浏览器定位被拒绝时是否弹提示（引导开权限）
   */
  locate: (showPrompt?: boolean) => Promise<UserLocation | null>
  /** 独立 IP 兜底（locate 内部已含；导出供降级路径单独使用） */
  locateByIp: () => Promise<UserLocation | null>
  /** 清除缓存与当前值（"刷新定位"前先调用，否则永远命中缓存） */
  clearLocation: () => void
}

/**
 * 创建定位解析器。三级兜底的关键语义：
 * 1. 缓存命中即返回（所以"强制刷新"必须先 clearLocation）；
 * 2. 浏览器精确定位成功但逆地编失败 → 继续走 IP 兜底，不整体失败；
 * 3. 全部失败返回 null（调用方提示手动选择），绝不编造位置。
 */
export function createUserLocationResolver(source: LocationSource): UserLocationResolver {
  const state = { current: null as UserLocation | null }

  async function locateByIp(): Promise<UserLocation | null> {
    try {
      const d = await source.ipLocate()
      if (d?.city && d.city !== '局域网') {
        const loc: UserLocation = { lat: null, lng: null, city: d.city, province: d.province || d.city }
        persist(loc)
        state.current = loc
        return loc
      }
    } catch {
      /* IP 定位失败不阻塞 */
    }
    return null
  }

  async function locate(showPrompt = false): Promise<UserLocation | null> {
    const cached = readCache()
    if (cached) {
      state.current = cached
      return cached
    }
    const pos = await geolocate()
    if (pos) {
      try {
        const data = await source.reverseGeocode(pos.coords.latitude, pos.coords.longitude)
        if (data.city) {
          const loc: UserLocation = {
            lat: pos.coords.latitude,
            lng: pos.coords.longitude,
            city: data.city,
            province: data.province || data.city,
          }
          persist(loc)
          state.current = loc
          return loc
        }
      } catch {
        /* 逆地编失败 → IP 兜底 */
      }
    } else if (showPrompt) {
      alert('请允许浏览器获取位置信息，或手动选择所在城市。')
    }
    return locateByIp()
  }

  function clearLocation(): void {
    localStorage.removeItem(LOCATION_CACHE_KEY)
    state.current = null
  }

  return {
    get userLocation() {
      return state.current
    },
    locate,
    locateByIp,
    clearLocation,
  }
}

/* ------------------------------------------------------------
 * 高德接入示例（放进项目自己的 api 模块，密钥走 .env / 后端代理）：
 *
 *   async function ipLocate() {
 *     const r = await fetch('/api/geo/ip-locate').then((r) => r.json())
 *     return r.city ? r : null        // 后端对私有 IP 应返回空（不编造"局域网"）
 *   }
 *   async function reverseGeocode(lat: number, lng: number) {
 *     const r = await fetch(`/api/geo/regeo?lat=${lat}&lng=${lng}`).then((r) => r.json())
 *     return { city: r.city ?? '', province: r.province ?? '' }
 *   }
 *   const resolver = createUserLocationResolver({ ipLocate, reverseGeocode })
 * ------------------------------------------------------------ */
