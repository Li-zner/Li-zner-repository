/** 独立 RAG 控制台构建：只打进必要页面，不携带主聊天 bundle。 */
import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  base: process.env.VITE_CONSOLE_BASE || '/',
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  build: {
    outDir: 'dist-console',
    emptyOutDir: true,
    rollupOptions: {
      input: fileURLToPath(new URL('./console.html', import.meta.url)),
    },
  },
})
