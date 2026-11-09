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

// 会话全量转录: 每轮问题/补充/终答全文 + 引用(HITL/要素同响应带出)
export const getTranscript = (sid) =>
  http.get(`/sessions/${sid}/transcript`).then((r) => r.data)

// 会话 JSON 导出到后端本地 exports/ 目录(返回 file/bytes/sha256)
export const exportSession = (sid) =>
  http.post(`/sessions/${sid}/export`).then((r) => r.data)

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

// minorStates v2: 时间轴(chain=1 串起 HITL 续跑父 run)、增量内容行、
// 阶段详情、阈值配置(前端不硬编码标定值)
export const getMonitorTimeline = (runId, chain = true) =>
  http
    .get(`/monitor/runs/${runId}/timeline`, { params: { chain: chain ? 1 : 0 } })
    .then((r) => r.data)

export const getMonitorMinorStates = (runId, afterStageId = 0) =>
  http
    .get(`/monitor/runs/${runId}/minor-states`, {
      params: { after_stage_id: afterStageId },
    })
    .then((r) => r.data)

export const getMonitorStageDetail = (stageId) =>
  http.get(`/monitor/stages/${stageId}/detail`).then((r) => r.data)

export const getMonitorConfig = () =>
  http.get('/monitor/config').then((r) => r.data)

export const getMonitorEvals = () =>
  http.get('/monitor/evals').then((r) => r.data)

// 统一审计视图: 一次请求拿结论/时间轴/证据/上下文/性能
export const getAuditView = (runId, { chain = true, afterStageId = 0 } = {}) =>
  http
    .get(`/monitor/runs/${runId}/audit-view`, {
      params: { chain: chain ? 1 : 0, after_stage_id: afterStageId },
    })
    .then((r) => r.data)

// 节点审计详情: 默认不含 raw(prompt/完整 state), 原始数据 tab 传 includeRaw=true
export const getAuditDetail = (stageId, includeRaw = false) =>
  http
    .get(`/monitor/stages/${stageId}/audit-detail`, {
      params: { include_raw: includeRaw ? 1 : 0 },
    })
    .then((r) => r.data)
