<script setup>
import { onMounted } from 'vue'
import { state, resetTurn, setSession } from './store'
import { askAttorney, askAssistant } from './api'
import { streamConsult } from './sse'
import ModeSwitch from './components/ModeSwitch.vue'
import DisclaimerToast from './components/DisclaimerToast.vue'
import HistorySidebar from './components/HistorySidebar.vue'
import ChatView from './components/ChatView.vue'
import ThinkingPanel from './components/ThinkingPanel.vue'
import ToolTimeline from './components/ToolTimeline.vue'
import ElementPanel from './components/ElementPanel.vue'
import CitationList from './components/CitationList.vue'
import InterruptPanel from './components/InterruptPanel.vue'
import DocComposer from './components/DocComposer.vue'
import DotPattern from './components/inspira/DotPattern.vue'
import TypingAnimation from './components/inspira/TypingAnimation.vue'

onMounted(() => {
  /* HistorySidebar 数据拉取由其自身 onMounted 负责 */
})

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
    await streamConsult(url, (e) => {
      if (e.event === 'reasoning') {
        state.value.reasoningActive = true
        state.value.reasoning += e.data.delta
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
      else if (e.event === 'error') {
        state.value.error = String(e.data)
        state.value.reasoningError = true
      } else if (e.event === 'done') {
        assistant.done = true
        state.value.reasoningActive = false
      }
    })
  } catch (err) {
    state.value.error = String(err)
  } finally {
    state.value.busy = false
    state.value.reasoningActive = false
    assistant.done = true
  }
}

// HITL resume 返回: 有新 interrupt 继续挂面板, 有 final_answer 追加为 assistant 消息
function onResumed(r) {
  state.value.interrupt = r.interrupt || null
  if (r.final_answer)
    state.value.messages.push({
      role: 'assistant',
      text: r.final_answer,
      collapsed: false,
      done: true,
    })
  if (r.session_id) setSession(r.session_id)
}
</script>

<template>
  <div class="relative flex h-screen bg-slate-100">
    <DotPattern class="opacity-60" />
    <HistorySidebar class="w-64 shrink-0 border-r bg-white/80 backdrop-blur z-10" />
    <div class="flex-1 flex flex-col z-10">
      <header class="flex items-center gap-3 p-3 border-b bg-white/80">
        <ModeSwitch />
        <span class="text-sm text-slate-400">会话: {{ state.sessionId || '(新建)' }}</span>
      </header>
      <DisclaimerToast />
      <main class="flex-1 overflow-y-auto p-4">
        <div v-if="!state.messages.length" class="text-slate-500 text-sm mt-8 text-center">
          <TypingAnimation
            :texts="['代理律师模式: 提问婚姻家事问题, 我来分析', '律师助理模式: 粘贴案情, 我来起草起诉状 / 答辩状']"
          />
        </div>
        <p
          v-if="state.error"
          class="text-red-600 text-sm border border-red-300 rounded p-2 bg-red-50"
        >
          出错: {{ state.error }} (不做兜底, 请修正后重试)
        </p>
        <ThinkingPanel />
        <ToolTimeline />
        <ChatView />
        <ElementPanel />
        <InterruptPanel
          v-if="state.interrupt"
          :interrupt="state.interrupt"
          @resumed="onResumed"
        />
      </main>
      <footer class="border-t p-3 bg-white/80">
        <!-- 统一提交区: attorney 单行输入, assistant 案情粘贴+文书单选 -->
        <DocComposer @submit="submit" />
      </footer>
    </div>
  </div>
</template>
