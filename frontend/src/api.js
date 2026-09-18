import axios from 'axios'

// 全部走 vite proxy 的 /api 前缀(见 vite.config.js), 生产部署由网关承担同样职责
const http = axios.create({ baseURL: '/api', timeout: 600000 })

export const askAttorney = (query, sessionId = '') =>
  http.post('/attorney/ask', { query, session_id: sessionId }).then((r) => r.data)

export const askAssistant = (caseDetails, docType, sessionId = '') =>
  http
    .post('/assistant/ask', {
      case_details: caseDetails,
      doc_type: docType,
      session_id: sessionId,
    })
    .then((r) => r.data)

export const resumeHITL = (sessionId, answer) =>
  http.post('/ask/resume', { session_id: sessionId, answer }).then((r) => r.data)

export const listSessions = () => http.get('/sessions').then((r) => r.data)

export const getSessionDetail = (sid) =>
  http.get(`/sessions/${sid}`).then((r) => r.data)

export const fetchDisclaimer = () =>
  http.get('/disclaimer').then((r) => r.data.disclaimer)
