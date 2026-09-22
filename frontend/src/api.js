import axios from 'axios'

// 全部走 vite proxy 的 /api 前缀(见 vite.config.js), 生产部署由网关承担同样职责
const http = axios.create({ baseURL: '/api', timeout: 600000 })

// L13: 删除未使用的 askAttorney / askAssistant / resumeHITL —— 流式路径
// 走 sse.js, 阻塞式端点前端已不再调用; 在用的保留

export const listSessions = () => http.get('/sessions').then((r) => r.data)

export const getSessionDetail = (sid) =>
  http.get(`/sessions/${sid}`).then((r) => r.data)

// 会话对话日志聚合端点(澄清轮次 + 确认决策 + 终答落点),
// 空会话/PG 掉线后端返回 200 空态, 由调用方归一为 null
export const getDialogue = (sid) =>
  http.get(`/sessions/${sid}/dialogue`).then((r) => r.data)

export const fetchDisclaimer = () =>
  http.get('/disclaimer').then((r) => r.data.disclaimer)
