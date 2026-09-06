/* 旅行助手 Service Worker
 * 策略：/assets/*（哈希文件名）缓存优先；页面导航网络优先、离线回退；
 * /api /v2 /auth（含 SSE 流）一律不拦截。 */
const CACHE = 'gw-shell-v1'

self.addEventListener('install', () => {
  self.skipWaiting()
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      // 清理旧版本缓存
      const keys = await caches.keys()
      await Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
      await self.clients.claim()
    })(),
  )
})

self.addEventListener('fetch', (event) => {
  const req = event.request
  if (req.method !== 'GET') return
  const url = new URL(req.url)
  if (url.origin !== location.origin) return
  // 后端接口与 SSE 流不缓存
  if (
    url.pathname.startsWith('/api/') ||
    url.pathname.startsWith('/v2/') ||
    url.pathname.startsWith('/auth/')
  ) {
    return
  }

  // 哈希命名的构建产物：缓存优先（内容不可变）
  if (url.pathname.startsWith('/assets/')) {
    event.respondWith(
      caches.open(CACHE).then(async (cache) => {
        const hit = await cache.match(req)
        if (hit) return hit
        const resp = await fetch(req)
        if (resp.ok) cache.put(req, resp.clone())
        return resp
      }),
    )
    return
  }

  // 页面导航：网络优先，离线回退缓存的首页（SPA hash 路由只有一个入口）。
  // 仅根路径响应可写入 '/' 缓存键——协议页等其它导航若也写入会把离线 shell 覆盖成错误页面
  if (req.mode === 'navigate') {
    event.respondWith(
      (async () => {
        try {
          const resp = await fetch(req)
          if (resp.ok && url.pathname === '/') {
            const cache = await caches.open(CACHE)
            cache.put('/', resp.clone())
          }
          return resp
        } catch {
          const cache = await caches.open(CACHE)
          return (await cache.match('/')) ?? Response.error()
        }
      })(),
    )
  }
})
