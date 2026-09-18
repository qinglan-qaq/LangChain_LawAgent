<!--
  FadeIn — Inspira UI (https://inspira-ui.com) 风格组件
  官方站点页面不可达, 按 Inspira FadeIn 的公开行为手写:
  挂载后从 opacity 0 / y+10 淡入到原位(motion-v)。
-->
<script setup>
import { motion } from 'motion-v'

defineProps({
  delay: { type: Number, default: 0 },
  duration: { type: Number, default: 0.4 },
})
</script>

<template>
  <motion.div
    :initial="{ opacity: 0, y: 10 }"
    :animate="{ opacity: 1, y: 0 }"
    :transition="{ duration, delay }"
  >
    <slot />
  </motion.div>
</template>
