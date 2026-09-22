<script setup>
import { state, newSession, abortCurrentStream } from '../store'

const modes = [
  { id: 'attorney', label: '代理律师' },
  { id: 'assistant', label: '律师助理' },
]

function pick(id) {
  if (state.value.mode === id) return
  // 有流在跑先终止(触发的 AbortError 由 App.vue 按用户取消静默处理)
  if (state.value.busy) abortCurrentStream()
  state.value.mode = id
  // 任务5: 切模式自动新建会话(不再恢复旧模式 sid; newSession 内含清消息+resetTurn)
  newSession()
}
</script>

<template>
  <div id="mode-switch" class="flex rounded-lg border overflow-hidden text-sm">
    <button
      v-for="m in modes"
      :key="m.id"
      class="px-3 py-1 transition-colors"
      :class="state.mode === m.id ? 'bg-slate-900 text-white' : 'bg-white text-slate-600'"
      @click="pick(m.id)"
    >
      {{ m.label }}
    </button>
  </div>
</template>
