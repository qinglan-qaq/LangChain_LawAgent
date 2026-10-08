<script setup>
import { computed } from 'vue'
import { state } from '../store'

// agent 运行状态栏(需求6): 常驻 footer 上方, 就绪→思考中→正在调用工具→
// 组合资料(后端 status 文本)→输出中→就绪; 数据全部复用现有回合状态,
// 不新增后端事件
const lastStreaming = computed(
  () =>
    [...state.value.messages]
      .reverse()
      .find((m) => m.role === 'assistant' && !m.kind && !m.done) || null,
)

const phase = computed(() => {
  if (!state.value.busy) return { dot: 'bg-emerald-500', text: '就绪', spin: false }
  const pendingTool = [...state.value.tools].reverse().find((t) => !t.result)
  if (pendingTool) {
    return { dot: 'bg-blue-500', text: `正在调用 ${pendingTool.name}…`, spin: true }
  }
  if (lastStreaming.value && lastStreaming.value.text) {
    return { dot: 'bg-blue-500', text: '输出中…', spin: true }
  }
  if (state.value.status) return { dot: 'bg-blue-500', text: state.value.status, spin: true }
  return { dot: 'bg-blue-500', text: '思考中…', spin: true }
})

const thinkingCount = computed(
  () =>
    (state.value.tools.length ? `工具 ${state.value.tools.length}` : '') ||
    (state.value.steps.length ? `步骤 ${state.value.steps.length}` : '') ||
    (state.value.planText ? '规划中' : ''),
)
</script>

<template>
  <div
    id="agent-status-bar"
    class="agent-status-bar flex items-center gap-2 px-4 py-1.5 text-xs border-t bg-white/90"
  >
    <span class="inline-block h-2 w-2 shrink-0 rounded-full" :class="phase.dot" />
    <span v-if="phase.spin" class="inline-block h-3 w-3 shrink-0 rounded-full border-2 border-slate-300 border-t-slate-600 animate-spin" />
    <span class="text-slate-600">{{ phase.text }}</span>
    <span v-if="state.busy && thinkingCount" class="text-slate-400">{{ thinkingCount }}</span>
  </div>
</template>
