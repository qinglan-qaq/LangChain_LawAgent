<script setup>
import { Motion } from 'motion-v'
import { state } from '../store'
</script>

<template>
  <!-- 正在执行指示器: 官方 Inspira 未移植紧凑 spinner 组件,
       按 ToolTimeline 同策略用官方动画底座 motion-v 直写入场 + Tailwind animate-spin -->
  <Motion
    v-if="state.status"
    as="div"
    class="flex items-center gap-2 my-1 text-xs text-slate-500 border rounded-lg px-2 py-1.5 bg-slate-50"
    :initial="{ opacity: 0, y: -4 }"
    :animate="{ opacity: 1, y: 0 }"
  >
    <span
      class="inline-block h-3 w-3 rounded-full border-2 border-slate-300 border-t-slate-600 animate-spin"
    />
    <span>{{ state.status }}</span>
  </Motion>
</template>
