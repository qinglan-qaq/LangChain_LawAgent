<script setup>
import { onMounted } from 'vue'
import { state, resetTurn, setSession, abortController, COLLAPSE_LEN, sessionsTick } from './store'
import { streamConsult, streamResume } from './sse'
import { getSessionDetail } from './api'
import ModeSwitch from './components/ModeSwitch.vue'
import DisclaimerToast from './components/DisclaimerToast.vue'
import HistorySidebar from './components/HistorySidebar.vue'
import ChatView from './components/ChatView.vue'
import ThinkingBox from './components/ThinkingBox.vue'
import ElementPanel from './components/ElementPanel.vue'
import CitationList from './components/CitationList.vue'
import InterruptPanel from './components/InterruptPanel.vue'
import DocComposer from './components/DocComposer.vue'
import { PatternBackground } from './components/inspira/pattern-background'
import TypewriterText from './components/inspira/TypewriterText.vue'

onMounted(() => {
  /* HistorySidebar 数据拉取由其自身 onMounted 负责 */
})

// SSE 事件路由(提问流与 HITL 恢复流共用):
// reasoning 按 source 分流(status 工作状态 / *_plan 计划内容 / 其余 CoT),
// token 进当轮 assistant 气泡, interrupt 挂 HITL 面板, answer 收终答
function handleStreamEvent(e, assistant) {
  if (e.event === 'reasoning') {
    const { source, delta } = e.data
    if (source === 'status') state.value.status = delta // 工作状态覆盖式更新
    else if (source && source.endsWith('_plan')) state.value.planText += delta // 计划内容流
    else {
      state.value.reasoningActive = true
      state.value.reasoning += delta
    }
  } else if (e.event === 'token') assistant.text += e.data
  else if (e.event === 'tool_call') state.value.tools.push({ name: e.data })
  else if (e.event === 'tool_result')
    state.value.tools[state.value.tools.length - 1] &&
      (state.value.tools[state.value.tools.length - 1].result = e.data)
  else if (e.event === 'elements') state.value.elements = e.data
  else if (e.event === 'progress') {
    // 执行进度(需求2): "[2/5] 检索婚姻法条文" → 解析当前步/总步数, 重建 steps
    const text = String(e.data || '')
    const m = /^\[(\d+)\/(\d+)\]/.exec(text)
    if (m) {
      const cur = Number(m[1])
      const total = Number(m[2])
      const steps = state.value.steps
      // 补齐总槽位(保留已见文本, 未到的步骤先用占位符)
      while (steps.length < total) steps.push({ text: `步骤 ${steps.length + 1}`, done: false, current: false })
      if (steps.length > total) steps.length = total
      steps[cur - 1].text = text.replace(/^\[\d+\/\d+\]\s*/, '')
      steps.forEach((s, i) => {
        s.done = i + 1 < cur
        s.current = i + 1 === cur
      })
    }
  } else if (e.event === 'prompts_record') {
    // 提示词记录(需求5): 单行截断 200 字符逐行追加
    state.value.promptsLog +=
      (typeof e.data === 'string' ? e.data : JSON.stringify(e.data)).slice(0, 200) + '\n'
  } else if (e.event === 'final_prompts') {
    // 最终提示词: 同 prompts_record, 加「【最终】」前缀区分
    state.value.promptsLog +=
      ('【最终】' + (typeof e.data === 'string' ? e.data : JSON.stringify(e.data))).slice(0, 200) + '\n'
  } else if (e.event === 'interrupt') {
    state.value.interrupt = e.data
    // HITL 问答记录(需求3): 追问以独立消息入流, 便于完整回看对话
    state.value.messages.push({
      role: 'assistant',
      kind: 'hitl_question',
      hitl_type: e.data.type || '',
      text: e.data.question || e.data.message || '请确认',
      options: e.data.options || null,
      done: true,
    })
    assistant.done = true
  } else if (e.event === 'answer') assistant.text = e.data
  else if (e.event === 'session_id') {
    setSession(e.data)
    sessionsTick.value++ // L11: 新会话建立后刷新会话列表
  } else if (e.event === 'tool_usage') state.value.toolUsage = e.data
  else if (e.event === 'error') {
    state.value.error = String(e.data)
    state.value.reasoningError = true
  } else if (e.event === 'done') {
    assistant.done = true
    state.value.reasoningActive = false
    state.value.status = ''
    // 终止帧: 全部步骤标记完成(执行进度收尾)
    state.value.steps.forEach((s) => {
      s.done = true
      s.current = false
    })
  }
}

// AbortError = 用户主动取消(切模式/中止), 不显示错误横幅
const isAbort = (err) => err && err.name === 'AbortError'

async function submit({ text, docType }) {
  resetTurn()
  state.value.busy = true
  state.value.error = ''
  state.value.messages.push({ role: 'user', text, collapsed: text.length > COLLAPSE_LEN, done: true })
  state.value.messages.push({ role: 'assistant', text: '', done: false })
  const isAttorney = state.value.mode === 'attorney'
  // M11: assistant 模式案情走 POST body(4000 CJK 经 URL 会超浏览器/代理长度
  // 限制); attorney 保持 GET 不动
  let url
  let reqBody = null
  if (isAttorney) {
    url = `/api/attorney/ask/stream?query=${encodeURIComponent(text)}&session_id=${encodeURIComponent(state.value.sessionId || '')}`
  } else {
    url = '/api/assistant/ask/stream'
    reqBody = {
      case_details: text,
      doc_type: docType || 'complaint',
      session_id: state.value.sessionId || '',
    }
  }
  const assistant = state.value.messages[state.value.messages.length - 1]
  let sawEvent = false // 校验失败回滚空消息用: 未收到任何流事件前的失败视为请求未成立
  const controller = new AbortController()
  abortController.value = controller
  try {
    await streamConsult(url, (e) => {
      sawEvent = true
      handleStreamEvent(e, assistant)
    }, controller.signal, reqBody)
  } catch (err) {
    if (isAbort(err)) {
      // 用户取消: 静默, 保留已生成的部分内容
    } else {
      state.value.error = String(err)
      // 校验/连接失败(checkOk 拒绝或同步抛错): 回滚刚 push 的 user+空气泡
      if (!sawEvent) state.value.messages.splice(-2)
    }
  } finally {
    abortController.value = null
    state.value.busy = false
    state.value.reasoningActive = false
    state.value.status = ''
    assistant.done = true
  }
}

// HITL 恢复(流式): resume 后的 planner CoT/状态/工具/interrupt/answer 全程实时下发
// 失败保护(H11): interrupt 不预清, 首个成功流事件后才清; 失败/断流时恢复面板(本地副本优先, 服务端兜底)
async function resumeHITL(answer) {
  const savedInterrupt = state.value.interrupt // 失败恢复用(首个成功流事件前不清)
  // HITL 问答记录(需求3): 用户回答入消息流(跳过时记「(跳过)」), 头部带所答问题摘要
  state.value.messages.push({
    role: 'user',
    kind: 'hitl_answer',
    text: answer || '(跳过)',
    question: savedInterrupt?.question || savedInterrupt?.message || '',
    done: true,
  })
  state.value.busy = true
  state.value.error = ''
  state.value.messages.push({ role: 'assistant', text: '', done: false })
  const assistant = state.value.messages[state.value.messages.length - 1]
  let cleared = false // 收到首个成功流事件后置位(同时清旧面板)
  let sawTerminal = false // 终止帧(interrupt/done): 判定流是否完整走完
  let aborted = false // 用户取消(切模式): resetTurn 已按意图清面板, 不恢复
  const controller = new AbortController()
  abortController.value = controller
  const onEvent = (e) => {
    if (!cleared && e.event !== 'error') {
      cleared = true
      state.value.interrupt = null // 流已正常建立, 清旧面板(新 interrupt 由事件再挂回)
    }
    if (e.event === 'interrupt' || e.event === 'done') sawTerminal = true
    handleStreamEvent(e, assistant)
  }
  try {
    await streamResume(answer, state.value.sessionId, onEvent, controller.signal)
  } catch (err) {
    aborted = isAbort(err)
    if (!aborted) state.value.error = String(err)
    // AbortError = 用户取消: 静默, 不显示错误横幅
  } finally {
    abortController.value = null
    state.value.busy = false
    state.value.reasoningActive = false
    state.value.status = ''
    // 流未走到终止帧且面板已消失: 恢复 interrupt(本地副本优先, 服务端兜底)
    if (!sawTerminal && !state.value.interrupt) {
      if (aborted) {
        // 用户取消(切模式 resetTurn 已清面板): 不恢复
      } else if (!cleared && savedInterrupt) {
        state.value.interrupt = savedInterrupt
        state.value.messages.pop() // 流未成立: 撤掉空气泡, 面板回来可重答
      } else {
        await restoreInterruptFromServer(state.value.sessionId)
      }
    }
    if (!state.value.interrupt) assistant.done = true
  }
}

// 服务端兜底恢复 HITL 面板: GET /sessions/{sid} 返回 pending interrupt(无则面板不恢复)
async function restoreInterruptFromServer(sid) {
  if (!sid) return
  try {
    const detail = await getSessionDetail(sid)
    if (detail && detail.interrupt) state.value.interrupt = detail.interrupt
  } catch {
    /* 兜底恢复失败保持现状(失败原因已由上游写入 state.error) */
  }
}
</script>

<template>
  <div class="relative flex h-screen bg-slate-100">
    <!-- 官方组件根节点自带 relative(cva 基底, class 数组不合并), 定位类由外层 wrapper 承载避免冲突 -->
    <div class="pointer-events-none absolute inset-0 z-0 opacity-60">
      <PatternBackground variant="dot" class="h-full" />
    </div>
    <HistorySidebar class="w-64 shrink-0 border-r bg-white/80 backdrop-blur z-10" />
    <div class="flex-1 flex flex-col z-10">
      <header class="flex items-center gap-3 p-3 border-b bg-white/80">
        <ModeSwitch />
        <span class="text-sm text-slate-400">会话: {{ state.sessionId || '(新建)' }}</span>
      </header>
      <DisclaimerToast />
      <main class="flex-1 overflow-y-auto p-4">
        <div v-if="!state.messages.length" class="text-slate-500 text-sm mt-8 text-center">
          <TypewriterText
            :text="['代理律师模式: 提问婚姻家事问题, 我来分析', '律师助理模式: 粘贴案情, 我来起草起诉状 / 答辩状']"
          />
        </div>
        <p
          v-if="state.error"
          class="text-red-600 text-sm border border-red-300 rounded p-2 bg-red-50"
        >
          出错: {{ state.error }} (不做兜底, 请修正后重试)
        </p>
        <ThinkingBox />
        <ChatView />
        <ElementPanel />
        <InterruptPanel
          v-if="state.interrupt"
          :interrupt="state.interrupt"
          @resume="resumeHITL"
        />
      </main>
      <footer class="border-t p-3 bg-white/80">
        <!-- 统一提交区: attorney 单行输入, assistant 案情粘贴+文书单选 -->
        <DocComposer @submit="submit" />
      </footer>
    </div>
  </div>
</template>
