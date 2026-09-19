<script setup>
import { Motion } from 'motion-v'
import { state } from '../store'
</script>

<template>
  <!-- 工具调用紧凑日志: 官方 Inspira 无对应组件(Timeline 为全页滚动式/AnimatedList 为
       轮播式), 按官方动画底座 motion-v 直写逐项入场 -->
  <div
    v-if="state.tools.length"
    class="my-2 text-xs text-slate-600 border rounded-lg p-2 bg-slate-50"
  >
    <div class="mb-1 text-slate-500">工具调用</div>
    <ol>
      <Motion
        v-for="(item, i) in state.tools"
        :key="i"
        as="li"
        class="flex items-center gap-2 py-0.5"
        :initial="{ opacity: 0, y: 6 }"
        :animate="{ opacity: 1, y: 0 }"
      >
        <span class="text-slate-400">[{{ i + 1 }}]</span>
        <span class="font-mono">{{ item.name }}</span>
        <span v-if="item.result" class="truncate text-slate-500">{{ item.result }}</span>
      </Motion>
    </ol>
  </div>
</template>
