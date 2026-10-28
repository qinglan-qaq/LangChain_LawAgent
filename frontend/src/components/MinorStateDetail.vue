<!-- MinorStateDetail.vue
  阶段详情面板(D-spec §六): 点击时间轴节点后在时间轴下方展示 ——
  输入/输出投影、output_json、context meta、route/metrics/error、span 原文、
  完整终答与 refs 解析; RAG 分数条阈值来自 /monitor/config(不硬编码)。
-->
<script setup>
import { computed } from 'vue'
import ContextMetaPanel from './ContextMetaPanel.vue'
import JsonTree from './JsonTree.vue'
import { fmtCST } from '../lib/time'

const props = defineProps({
  detail: { type: Object, default: null }, // MonitorStageDetail
  loading: { type: Boolean, default: false },
  config: { type: Object, default: () => ({}) },
})

const state = computed(() => props.detail?.state || null)
const span = computed(() => props.detail?.span || null)

const phaseText = computed(() => {
  const p = state.value?.phase
  return p === 'waiting_human' ? '等待人工' : p === 'running' ? '执行中' : '完成'
})

function pickScore(d) {
  for (const k of ['relevance_score', 'rerank_score', 'hybrid_score', 'score']) {
    if (d?.[k] != null) return Number(d[k])
  }
  return null
}

const ragDocs = computed(() => {
  const candidates = [
    state.value?.output_json?.rag_documents,
    state.value?.output_json?.tool_result?.rag_documents,
    span.value?.output?.rag_documents,
    span.value?.output?.tool_result?.rag_documents,
  ]
  for (const c of candidates) {
    if (Array.isArray(c) && c.length) return c
  }
  return []
})

function scoreCls(sc) {
  if (sc == null) return 'bg-slate-300'
  const good = Number(props.config?.correct_threshold ?? 0.7)
  const mid = Number(props.config?.incorrect_threshold ?? 0.5)
  if (sc >= good) return 'bg-emerald-500'
  if (sc >= mid) return 'bg-amber-500'
  return 'bg-red-400'
}

function fmt(ms) {
  if (ms == null) return '—'
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(2)}s`
}
</script>

<template>
  <div id="minor-state-detail" class="minor-state-detail rounded-lg border border-slate-200 bg-white p-4">
    <div v-if="loading" class="py-8 text-center text-sm text-slate-400">加载阶段详情…</div>
    <div v-else-if="!state" id="minor-state-detail-empty"
         class="py-8 text-center text-sm text-slate-400">
      点击上方任意节点查看详细监控内容
    </div>

    <div v-else class="space-y-3">
      <header id="minor-state-detail-header" class="flex flex-wrap items-center gap-2 border-b pb-2">
        <h3 class="text-base font-semibold text-slate-700">
          {{ state.node_name }}
          <span v-if="state.seq > 1" class="text-xs font-normal text-slate-400">第 {{ state.seq }} 次</span>
        </h3>
        <span class="rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-500">{{ phaseText }}</span>
        <span class="rounded bg-slate-50 px-1.5 py-0.5 text-xs text-slate-500">
          {{ state.status }} · {{ fmt(state.latency_ms) }}
        </span>
        <span v-if="state.attempt > 1" class="rounded bg-indigo-50 px-1.5 py-0.5 text-xs text-indigo-600">
          续跑 attempt={{ state.attempt }}
        </span>
        <span v-if="state.content_source === 'fallback_placeholder'"
              class="rounded bg-orange-50 px-1.5 py-0.5 text-xs text-orange-600">
          占位行(minor 缺失)
        </span>
        <span class="ml-auto font-mono text-[11px] text-slate-400">
          stage #{{ state.stage_id }} · {{ fmtCST(state.started_at) }}
        </span>
      </header>

      <div v-if="state.route || state.error" class="flex flex-wrap gap-2 text-xs">
        <span v-if="state.route" class="rounded bg-blue-50 px-2 py-0.5 text-blue-700">
          路由 → {{ state.route }}<template v-if="state.route_reason">({{ state.route_reason }})</template>
        </span>
        <span v-if="state.error" class="rounded bg-red-50 px-2 py-0.5 text-red-700">
          错误: {{ state.error }}
        </span>
      </div>

      <div v-if="ragDocs.length" id="minor-state-rag" class="space-y-1">
        <p class="text-xs font-medium text-slate-500">
          检索结果分数(阈值: correct ≥ {{ config.correct_threshold ?? '—' }},
          incorrect ≥ {{ config.incorrect_threshold ?? '—' }})
        </p>
        <div v-for="(d, i) in ragDocs" :key="i" class="flex items-center gap-2 text-xs">
          <span class="w-56 truncate text-slate-600">
            {{ d.case_number || d.law_title || d.title || `doc-${i}` }}
          </span>
          <div class="h-2 flex-1 overflow-hidden rounded bg-slate-100">
            <div class="h-full rounded" :class="scoreCls(pickScore(d))"
                 :style="{ width: `${Math.min((pickScore(d) || 0) * 100, 100)}%` }" />
          </div>
          <span class="w-12 text-right text-slate-500">
            {{ pickScore(d) == null ? '—' : pickScore(d).toFixed(3) }}
          </span>
        </div>
      </div>

      <details id="minor-state-input" class="text-xs" open>
        <summary class="cursor-pointer font-medium text-slate-500 hover:text-slate-700">输入投影</summary>
        <pre class="mt-1 max-h-56 overflow-auto whitespace-pre-wrap rounded border bg-slate-50 p-2 text-[11px] text-slate-600">{{ state.input_text || '(无输入投影)' }}</pre>
      </details>

      <details id="minor-state-output" class="text-xs" open>
        <summary class="cursor-pointer font-medium text-slate-500 hover:text-slate-700">输出投影</summary>
        <pre class="mt-1 max-h-56 overflow-auto whitespace-pre-wrap rounded border bg-slate-50 p-2 text-[11px] text-slate-600">{{ state.output_text || '(无文本输出)' }}</pre>
      </details>

      <details id="minor-state-json" class="text-xs">
        <summary class="cursor-pointer font-medium text-slate-500 hover:text-slate-700">output_json</summary>
        <div class="mt-1 max-h-72 overflow-auto rounded border bg-slate-50 p-2 font-mono text-[11px]">
          <JsonTree :node="state.output_json" name="output_json" :default-open="1"
                    tree-id="minor-state-output-tree" />
        </div>
      </details>

      <details id="minor-state-metrics" class="text-xs">
        <summary class="cursor-pointer font-medium text-slate-500 hover:text-slate-700">metrics / refs</summary>
        <div class="mt-1 grid gap-2 md:grid-cols-2">
          <div class="max-h-48 overflow-auto rounded border bg-slate-50 p-2 font-mono text-[11px]">
            <JsonTree :node="state.metrics" name="metrics" :default-open="1"
                      tree-id="minor-state-metrics-tree" />
          </div>
          <div class="max-h-48 overflow-auto rounded border bg-slate-50 p-2 font-mono text-[11px]">
            <JsonTree :node="state.refs" name="refs" :default-open="1"
                      tree-id="minor-state-refs-tree" />
          </div>
        </div>
      </details>

      <details id="minor-state-context" class="text-xs" open>
        <summary class="cursor-pointer font-medium text-slate-500 hover:text-slate-700">上下文信封 meta</summary>
        <div class="mt-1">
          <ContextMetaPanel :meta="state.context_meta" />
        </div>
      </details>

      <details v-if="span" id="minor-state-span" class="text-xs">
        <summary class="cursor-pointer font-medium text-slate-500 hover:text-slate-700">
          span 原文 ({{ span.name }} · {{ span.status }})
        </summary>
        <div class="mt-1 grid gap-2 md:grid-cols-3">
          <div v-for="k in ['input', 'output', 'state']" :key="k"
               class="max-h-72 overflow-auto rounded border bg-slate-50 p-2 font-mono text-[11px]">
            <p class="mb-1 text-[10px] uppercase tracking-wide text-slate-400">{{ k }}</p>
            <JsonTree :node="span[k]" :name="k" :default-open="0"
                      :tree-id="`minor-state-span-${k}`" />
          </div>
        </div>
      </details>

      <details v-if="detail.final_answer" id="minor-state-final-answer" class="text-xs">
        <summary class="cursor-pointer font-medium text-slate-500 hover:text-slate-700">run 完整终答</summary>
        <pre class="mt-1 max-h-72 overflow-auto whitespace-pre-wrap rounded border bg-slate-50 p-2 text-[11px] text-slate-600">{{ detail.final_answer }}</pre>
      </details>

      <details v-if="detail.refs_resolved && Object.keys(detail.refs_resolved).length"
               id="minor-state-refs-resolved" class="text-xs">
        <summary class="cursor-pointer font-medium text-slate-500 hover:text-slate-700">refs 解析</summary>
        <div class="mt-1 max-h-72 overflow-auto rounded border bg-slate-50 p-2 font-mono text-[11px]">
          <JsonTree :node="detail.refs_resolved" name="refs_resolved" :default-open="0"
                    tree-id="minor-state-refs-resolved-tree" />
        </div>
      </details>
    </div>
  </div>
</template>
