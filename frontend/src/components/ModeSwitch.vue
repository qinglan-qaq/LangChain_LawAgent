<script setup>
import { state, resetTurn, abortCurrentStream } from '../store'

const modes = [
  { id: 'attorney', label: '代理律师' },
  { id: 'assistant', label: '律师助理' },
]

function pick(id) {
  if (state.value.mode === id) return
  // 有流在跑先终止(触发的 AbortError 由 App.vue 按用户取消静默处理), 再重置回合
  if (state.value.busy) abortCurrentStream()
  state.value.mode = id
  resetTurn()
}
</script>

<template>
  <div class="flex rounded-lg border overflow-hidden text-sm">
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
