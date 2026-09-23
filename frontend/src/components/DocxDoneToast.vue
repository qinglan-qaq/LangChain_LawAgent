<script setup>
import { onBeforeUnmount, onMounted } from 'vue'
import { state } from '../store'
// 常驻挂载方案: App 挂一次, state.docxToast 控制显隐;
// 8s 计时从组件挂载起算, 到点自动收通知(用户点下载也收)
let timer = null
onMounted(() => {
  timer = setTimeout(() => (state.value.docxToast = false), 8000)
})
onBeforeUnmount(() => timer && clearTimeout(timer))
</script>
<template>
  <!-- docx 完成通知: 左上角, 内嵌下载入口(8s 自消) -->
  <div
    v-if="state.docxToast"
    class="fixed top-4 left-4 max-w-xs z-50 border border-amber-300 rounded-xl bg-white shadow-lg p-3 text-xs"
  >
    <div class="font-semibold text-slate-800 mb-1">Word 文书生成完成</div>
    <div class="text-slate-500 mb-2">可在消息区下载, 或点击此处下载</div>
    <a
      id="btn-download-docx-toast"
      class="inline-block px-3 py-1 rounded bg-amber-600 text-white"
      :href="'/api/sessions/' + encodeURIComponent(state.sessionId) + '/docx/latest'"
      download
      @click="state.docxToast = false"
    >
      下载文书
    </a>
  </div>
</template>
