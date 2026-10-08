<!-- StageDetailDrawer.vue
  阶段内容全景(D7/D8): 甘特条点击展开 —— 该节点关联 spans 的
  input/output/state/token 折叠面板; rag_documents 逐条 hybrid_score 分数条
  (correct≥0.5 绿 / ambiguous≥0.2 黄 / incorrect 红)。
-->
<script setup>
import { computed } from 'vue'

const props = defineProps({
  stage: { type: Object, default: null },      // MonitorStage
  spans: { type: Array, default: () => [] },   // 该 run 全部 span
})

const related = computed(() =>
  props.stage ? props.spans.filter((x) => x.name === props.stage.node_name) : []
)
const ragDocs = computed(() => {
  for (const s of related.value) {
    const docs = s?.output?.rag_documents || s?.output?.tool_result?.rag_documents
    if (Array.isArray(docs) && docs.length) return docs
  }
  return []
})
function scoreCls(sc) {
  return sc >= 0.5 ? 'bg-emerald-500' : sc >= 0.2 ? 'bg-amber-500' : 'bg-red-400'
}
const secs = [
  { key: 'input', label: '输入 (入参 state)' },
  { key: 'output', label: '输出 (工具结果/节点产出)' },
  { key: 'state', label: '执行后 state 快照' },
]
</script>

<template>
  <div v-if="stage" class="border rounded bg-white p-4 space-y-3">
    <h3 class="font-semibold text-slate-700">
      {{ stage.node_name }}
      <template v-if="stage.seq > 1">(第{{ stage.seq }}次)</template>
      <span class="ml-2 text-xs text-slate-400">
        {{ stage.status }} · {{ stage.latency_ms ?? '?' }}ms ·
        {{ stage.started_at }} → {{ stage.ended_at || '进行中' }}
      </span>
    </h3>

    <div v-if="ragDocs.length" class="space-y-1">
      <p class="text-xs text-slate-500 font-medium">检索结果 (hybrid_score 分数条)</p>
      <div v-for="(d, i) in ragDocs" :key="i" class="flex items-center gap-2 text-xs">
        <span class="w-56 truncate text-slate-600">
          {{ d.case_number || d.law_title || `doc-${i}` }}
        </span>
        <div class="flex-1 h-2 bg-slate-100 rounded">
          <div class="h-2 rounded" :class="scoreCls(d.hybrid_score || 0)"
               :style="{ width: `${Math.min((d.hybrid_score || 0) * 100, 100)}%` }" />
        </div>
        <span class="w-10 text-right text-slate-500">
          {{ (d.hybrid_score || 0).toFixed(3) }}
        </span>
      </div>
    </div>

    <details v-for="sec in secs" :key="sec.key" class="text-xs">
      <summary class="cursor-pointer text-slate-500 hover:text-slate-700">
        {{ sec.label }}
      </summary>
      <pre class="mt-1 max-h-64 overflow-auto bg-slate-50 border rounded p-2
text-[11px] leading-tight whitespace-pre-wrap">{{ JSON.stringify(related.map(s => s[sec.key]), null, 1) }}</pre>
    </details>

    <details class="text-xs">
      <summary class="cursor-pointer text-slate-500 hover:text-slate-700">token 用量</summary>
      <pre class="mt-1 bg-slate-50 border rounded p-2 text-[11px]">{{
        JSON.stringify(related.map(s => s.token_usage), null, 1)
      }}</pre>
    </details>
  </div>
</template>
