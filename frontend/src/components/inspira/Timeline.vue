<!--
  Timeline — Inspira UI (https://inspira-ui.com) 风格组件
  官方站点页面不可达, 按 Inspira Timeline 的公开行为手写:
  列表逐项淡入上浮(motion-v), 插槽渲染每行内容。
-->
<script setup>
import { motion } from 'motion-v'

defineProps({ items: { type: Array, default: () => [] } })
</script>

<template>
  <ol class="relative space-y-1">
    <motion.li
      v-for="(item, i) in items"
      :key="i"
      :initial="{ opacity: 0, y: 8 }"
      :animate="{ opacity: 1, y: 0 }"
      :transition="{ duration: 0.25, delay: i * 0.05 }"
      class="flex gap-2"
    >
      <slot :item="item" :index="i" />
    </motion.li>
  </ol>
</template>
