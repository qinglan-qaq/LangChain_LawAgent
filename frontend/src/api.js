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

// ========== 监控页(D-spec §五, Task 6 端点) ==========
export const getMonitorOverview = () =>
  http.get('/monitor/overview').then((r) => r.data)

// 分页: 后端 limit/offset 查询参数 + X-Total-Count 响应头, 归一为 {rows, total}
export const getMonitorRuns = (limit = 20, offset = 0) =>
  http
    .get('/monitor/runs', { params: { limit, offset } })
    .then((r) => ({ rows: r.data, total: parseInt(r.headers['x-total-count'] || '0', 10) }))

export const getMonitorRunDetail = (runId) =>
  http.get(`/monitor/runs/${runId}/stages`).then((r) => r.data)

export const getMonitorEvals = () =>
  http.get('/monitor/evals').then((r) => r.data)
