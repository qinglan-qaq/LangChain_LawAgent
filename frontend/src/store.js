import { ref } from 'vue'

// 会话 id 按模式分键存储(M13): 两模式共用一个 sid 会让 AT/AS 前缀与
// 端点模式不符(后端 400 session_mode_mismatch)且线程互串
const SID_KEYS = { attorney: 'lawapp_sid_attorney', assistant: 'lawapp_sid_assistant' }
const sidKey = () => SID_KEYS[state.value.mode] || SID_KEYS.attorney

export const state = ref({
  mode: 'attorney',          // attorney | assistant
  sessionId: localStorage.getItem(SID_KEYS.attorney) || '',
  disclaimerShown: localStorage.getItem('lawapp_disclaimer') === '1',
  messages: [],              // {role:'user'|'assistant', text, collapsed, done}
  reasoning: '',             // CoT 累积文本
  reasoningActive: false,
  reasoningError: false,
  planText: '',              // planner/replanner 计划内容实时流(source=*_plan)
  status: '',                // 当前工作状态(source=status, 覆盖式更新)
  toolUsage: {},             // 工具使用 JSON 记录 {toolName: [结果摘要]}
  tools: [],                 // 工具时间线 {name, result}
  elements: [],              // 要素面板
  interrupt: null,           // 当前 HITL 载荷
  busy: false,
  error: '',
})

// 按当前模式读取持久化的会话 id(切模式后调用, sessionId 自动切换到对应模式)
export function loadSession() {
  state.value.sessionId = localStorage.getItem(sidKey()) || ''
}

export function setSession(sid) {
  state.value.sessionId = sid
  if (sid) localStorage.setItem(sidKey(), sid)
}

export function markDisclaimerShown() {
  state.value.disclaimerShown = true
  localStorage.setItem('lawapp_disclaimer', '1')
}

export function resetTurn() {
  Object.assign(state.value, {
    reasoning: '', reasoningActive: false, reasoningError: false,
    planText: '', status: '', toolUsage: {},
    tools: [], elements: [], interrupt: null, error: '',
  })
}

// 在途 SSE 流的取消器: App.vue 每轮 submit/resume 新建, 流结束置空
export const abortController = ref(null)

// 终止在途 SSE 流(切模式等场景); 触发的 AbortError 由调用方按"用户取消"静默处理
export function abortCurrentStream() {
  if (abortController.value) {
    abortController.value.abort()
    abortController.value = null
  }
}

// 打字机效果(用户决策): token 流入 full, 显示层逐字追平
export function useTypewriter(full, shown, cps = 60) {
  let timer = null
  function start() {
    if (timer) return
    timer = setInterval(() => {
      if (shown.value.length >= full.value.length) {
        clearInterval(timer); timer = null; return
      }
      shown.value = full.value.slice(0, shown.value.length + 1)
    }, 1000 / cps)
  }
  return { start }
}
