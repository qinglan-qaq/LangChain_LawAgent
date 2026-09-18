<!--
  TypingAnimation — Inspira UI (https://inspira-ui.com) 风格组件
  官方站点页面不可达, 按 Magic UI TypingAnimation 的公开行为手写:
  逐字打出 texts 轮播, 打完停顿后删除换下一条, 循环往复。
-->
<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'

const props = defineProps({
  texts: { type: Array, default: () => [''] },
  speed: { type: Number, default: 120 }, // ms/字
  pause: { type: Number, default: 1500 }, // 全文展示停顿
})

const shown = ref('')
let timer = null
let ti = 0
let ci = 0
let deleting = false

function tick() {
  const t = props.texts[ti] ?? ''
  if (!deleting) {
    ci++
    shown.value = t.slice(0, ci)
    if (ci >= t.length) {
      deleting = true
      timer = setTimeout(tick, props.pause)
      return
    }
  } else {
    ci--
    shown.value = t.slice(0, Math.max(ci, 0))
    if (ci <= 0) {
      deleting = false
      ti = (ti + 1) % props.texts.length
    }
  }
  timer = setTimeout(tick, deleting ? props.speed / 2 : props.speed)
}

onMounted(tick)
onBeforeUnmount(() => clearTimeout(timer))
</script>

<template>
  <span class="whitespace-pre-wrap"
    ><span>{{ shown }}</span><span class="animate-pulse">▍</span></span
  >
</template>
