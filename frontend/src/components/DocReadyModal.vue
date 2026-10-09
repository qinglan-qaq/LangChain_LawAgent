<script setup>
import { state } from '../store'
// 文书生成完成模态: 双格式下载(Word 走原链路, PDF 走 Task 5 端点)
const fileUrl = (f) => '/api/sessions/' + encodeURIComponent(state.value.sessionId) + '/doc/' + f
const name = () => (state.value.docxPath || '').split(/[\\/]/).pop() || 'Word 文书'
function close() { state.value.showDocReady = false }
</script>
<template>
  <div v-if="state.showDocReady" id="doc-ready-modal"
       class="fixed inset-0 z-50 flex items-center justify-center bg-black/30"
       @click.self="close">
    <div class="doc-ready-card w-80 rounded-xl bg-white shadow-xl p-5 text-sm">
      <div class="font-semibold text-slate-800 mb-1">文书已生成</div>
      <div class="text-slate-500 mb-1 truncate" :title="name()">{{ name() }}</div>
      <div class="text-[10px] text-slate-400 mb-4">内容由 AI 生成, 需执业律师复核后使用</div>
      <div class="flex gap-2">
        <a id="btn-download-docx-modal" :href="fileUrl('docx')" download
           class="btn flex-1 rounded-lg bg-amber-600 text-white py-1.5 text-center"
           @click="close">下载 Word</a>
        <a id="btn-download-pdf-modal" :href="fileUrl('pdf')" download
           class="btn flex-1 rounded-lg bg-slate-700 text-white py-1.5 text-center"
           @click="close">下载 PDF</a>
      </div>
      <button id="btn-doc-ready-close"
              class="mt-3 w-full text-xs text-slate-400 underline"
              @click="close">关闭</button>
    </div>
  </div>
</template>
