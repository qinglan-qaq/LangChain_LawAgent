import { onBeforeUnmount, ref } from 'vue'

// 会话 id 按模式分键存储(M13): 两模式共用一个 sid 会让 AT/AS 前缀与
// 端点模式不符(后端 400 session_mode_mismatch)且线程互串
const SID_KEYS = { attorney: 'lawapp_sid_attorney', assistant: 'lawapp_sid_assistant' }
const sidKey = () => SID_KEYS[state.value.mode] || SID_KEYS.attorney

// L12: 用户消息折叠阈值(两处共用, 不再各自写 120 魔数)
export const COLLAPSE_LEN = 120

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
  steps: [],                 // 执行进度步骤 {text, done, current}(progress 事件 "[2/5] xx" 解析)
  promptsLog: '',            // 提示词记录(prompts_record/final_prompts 事件, 单行截断 200 字符)
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
    tools: [], steps: [], promptsLog: '',
    elements: [], interrupt: null, error: '',
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

// L11: 会话列表刷新信号 —— App.vue 收到 session_id 事件/HistorySidebar 切会话后
// 写入递增 tick, HistorySidebar watch 后重拉列表(单向通知, 不加轮询)
export const sessionsTick = ref(0)

// 打字机效果(用户决策): token 流入 full, 显示层逐字追平
// L10: 组件卸载时清 interval, 不再泄漏定时器
export function useTypewriter(full, shown, cps = 60) {
  let timer = null
  function stop() {
    if (timer) {
      clearInterval(timer)
      timer = null
    }
  }
  function start() {
    if (timer) return
    timer = setInterval(() => {
      if (shown.value.length >= full.value.length) {
        stop()
        return
      }
      shown.value = full.value.slice(0, shown.value.length + 1)
    }, 1000 / cps)
  }
  onBeforeUnmount(stop)
  return { start, stop }
}
