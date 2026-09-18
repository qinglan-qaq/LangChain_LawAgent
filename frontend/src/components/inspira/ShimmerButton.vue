<!--
  ShimmerButton — Inspira UI (https://inspira-ui.com) 风格组件
  官方站点页面不可达, 按 Magic UI ShimmerButton 的公开行为手写:
  深色按钮 + 白色高光带循环扫过(shimmer), 点击微缩反馈。
-->
<script setup>
defineProps({ disabled: { type: Boolean, default: false } })
</script>

<template>
  <button
    type="button"
    :disabled="disabled"
    class="relative overflow-hidden rounded-xl bg-slate-900 px-5 py-2 text-sm text-white
           transition-transform active:scale-[0.98]
           disabled:cursor-not-allowed disabled:opacity-50"
  >
    <span class="shimmer absolute inset-0" aria-hidden="true" />
    <span class="relative z-10"><slot>发送</slot></span>
  </button>
</template>

<style scoped>
.shimmer {
  background: linear-gradient(110deg, transparent 20%, rgba(255, 255, 255, 0.25) 40%, transparent 60%);
  transform: translateX(-100%);
  animation: shimmer 2.5s infinite;
}
@keyframes shimmer {
  100% {
    transform: translateX(100%);
  }
}
</style>
