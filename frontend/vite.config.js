import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'

// 后端: lawApp_LangGraph/FastAPI/api.py,默认监听 127.0.0.1:8000
// /api 前缀转发到后端,前端代码里写 fetch('/api/home') 即可,生产部署时由网关承担同样职责
// BACKEND_PORT 环境变量可覆盖目标端口(如 Windows 端口保留段占用 8000 时: BACKEND_PORT=8100)
const backendPort = process.env.BACKEND_PORT || '8000'
export default defineConfig({
  plugins: [vue(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: `http://127.0.0.1:${backendPort}`,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
