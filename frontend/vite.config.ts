import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发期代理到后端网关；生产构建产物由后端/FastAPI 直接挂载
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:10086',
      '/v2': 'http://localhost:10086',
      '/auth': 'http://localhost:10086',
      '/health': 'http://localhost:10086',
    },
  },
  // preview 与 dev 同代理：否则本地预览构建产物时 API 全 404（登录/支付等一律不可用）
  preview: {
    port: 4173,
    proxy: {
      '/api': 'http://localhost:10086',
      '/v2': 'http://localhost:10086',
      '/auth': 'http://localhost:10086',
      '/health': 'http://localhost:10086',
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['tests/**/*.test.ts'],
  },
} as never)
