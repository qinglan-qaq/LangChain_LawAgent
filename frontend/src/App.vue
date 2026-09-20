<script setup>
import { onMounted } from 'vue'
import { state, resetTurn, setSession } from './store'
import { streamConsult, streamResume } from './sse'
import ModeSwitch from './components/ModeSwitch.vue'
import DisclaimerToast from './components/DisclaimerToast.vue'
import HistorySidebar from './components/HistorySidebar.vue'
import ChatView from './components/ChatView.vue'
import ThinkingPanel from './components/ThinkingPanel.vue'
import ToolTimeline from './components/ToolTimeline.vue'
import StatusBar from './components/StatusBar.vue'
import PlanPanel from './components/PlanPanel.vue'
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
  else if (e.event === 'interrupt') {
    state.value.interrupt = e.data
    assistant.done = true
  } else if (e.event === 'answer') assistant.text = e.data
  else if (e.event === 'session_id') setSession(e.data)
  else if (e.event === 'tool_usage') state.value.toolUsage = e.data
  else if (e.event === 'error') {
    state.value.error = String(e.data)
    state.value.reasoningError = true
  } else if (e.event === 'done') {
    assistant.done = true
    state.value.reasoningActive = false
    state.value.status = ''
  }
}

async function submit({ text, docType }) {
  resetTurn()
  state.value.busy = true
  state.value.error = ''
  state.value.messages.push({ role: 'user', text, collapsed: text.length > 120, done: true })
  state.value.messages.push({ role: 'assistant', text: '', done: false })
  const isAttorney = state.value.mode === 'attorney'
  const url = isAttorney
    ? `/api/attorney/ask/stream?query=${encodeURIComponent(text)}&session_id=${encodeURIComponent(state.value.sessionId || '')}`
    : `/api/assistant/ask/stream?case_details=${encodeURIComponent(text)}&doc_type=${docType || 'complaint'}&session_id=${encodeURIComponent(state.value.sessionId || '')}`
  const assistant = state.value.messages[state.value.messages.length - 1]
  try {
    await streamConsult(url, (e) => handleStreamEvent(e, assistant))
  } catch (err) {
    state.value.error = String(err)
  } finally {
    state.value.busy = false
    state.value.reasoningActive = false
    state.value.status = ''
    assistant.done = true
  }
}

// HITL 恢复(流式): resume 后的 planner CoT/状态/工具/interrupt/answer 全程实时下发
async function resumeHITL(answer) {
  state.value.interrupt = null
  state.value.busy = true
  state.value.error = ''
  state.value.messages.push({ role: 'assistant', text: '', done: false })
  const assistant = state.value.messages[state.value.messages.length - 1]
  try {
    await streamResume(answer, state.value.sessionId, (e) => handleStreamEvent(e, assistant))
  } catch (err) {
    state.value.error = String(err)
  } finally {
    state.value.busy = false
    state.value.reasoningActive = false
    state.value.status = ''
    if (!state.value.interrupt) assistant.done = true
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
        <StatusBar />
        <ThinkingPanel />
        <PlanPanel />
        <ToolTimeline />
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
