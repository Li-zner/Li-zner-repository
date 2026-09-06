/** 路由与认证守卫：未登录一律去 /login（token 由 JWT 持有） */
import { createRouter, createWebHashHistory } from 'vue-router'
import { getAccessToken, setUnauthorizedHandler } from '../api/http'

const router = createRouter({
  // hash 路由：任何静态托管/直连端口都无需服务端回退配置（nginx try_files 为后续升级项）
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/chat' },
    { path: '/login', component: () => import('../views/LoginView.vue') },
    { path: '/chat', component: () => import('../views/ChatView.vue') },
    { path: '/map', component: () => import('../views/MapView.vue') },
  ],
})

router.beforeEach((to) => {
  if (to.path !== '/login' && !getAccessToken()) return { path: '/login' }
  if (to.path === '/login' && getAccessToken()) return { path: '/chat' }
})

/** 401 最终兜底：清态后回登录页（由 http 层回调） */
setUnauthorizedHandler(() => {
  router.push({ path: '/login', query: { error: 'unauthorized' } })
})

export default router
