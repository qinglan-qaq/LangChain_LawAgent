<script setup>
import { computed, ref } from 'vue'
import { state } from '../store'

const open = ref(true)
const head = computed(() => state.value.reasoning.slice(-60))
</script>

<template>
  <details
    :open="open"
    class="border rounded-lg p-2 my-1 text-xs bg-slate-50"
    :class="state.reasoningError ? 'border-red-400' : 'border-slate-200'"
  >
    <summary class="cursor-pointer select-none">
      {{ state.reasoningError ? '思考过程(中断, 已保留片段)' : '思考过程' }}
      <span v-if="state.reasoningActive" class="animate-pulse">▍</span>
    </summary>
    <pre class="whitespace-pre-wrap mt-1 text-slate-600">{{ state.reasoning || head }}</pre>
  </details>
</template>
