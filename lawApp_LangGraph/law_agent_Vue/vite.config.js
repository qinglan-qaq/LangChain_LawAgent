import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 后端: lawApp_LangGraph/FastAPI/api.py,默认监听 127.0.0.1:8000
// /api 前缀转发到后端,前端代码里写 fetch('/api/home') 即可,生产部署时由网关承担同样职责
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
