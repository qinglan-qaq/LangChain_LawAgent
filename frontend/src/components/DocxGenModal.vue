<script setup>
import { state } from '../store'
</script>
<template>
  <!-- docx 生成中全局弹窗: 遮罩阻断误操作, spinner + 提示文案 -->
  <div
    v-if="state.docxGenerating"
    class="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 backdrop-blur-sm"
  >
    <div class="bg-white rounded-2xl shadow-xl px-8 py-6 flex flex-col items-center gap-3">
      <div
        class="h-8 w-8 rounded-full border-2 border-amber-500 border-t-transparent animate-spin"
      ></div>
      <div class="text-sm text-slate-700">正在生成 Word 文书…</div>
      <div class="text-xs text-slate-400">按模板填充字段, 请勿关闭页面</div>
    </div>
  </div>
</template>
