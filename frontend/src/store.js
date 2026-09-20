import { ref } from 'vue'

export const state = ref({
  mode: 'attorney',          // attorney | assistant
  sessionId: localStorage.getItem('lawapp_sid') || '',
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

export function setSession(sid) {
  state.value.sessionId = sid
  if (sid) localStorage.setItem('lawapp_sid', sid)
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
