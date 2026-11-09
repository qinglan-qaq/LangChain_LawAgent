<!-- AuditTimeline.vue
  审计时间轴: 决策/HITL/终答各自成组, 执行步骤按计划步骤折叠。
  组内节点只展示摘要, 点击交给父层加载 audit-detail。
-->
<script setup>
import { computed } from 'vue'
import { fmtMs, STATUS_CLS } from './auditUi'

const props = defineProps({
  groups: { type: Array, default: () => [] },
  nodes: { type: Array, default: () => [] },
  selectedStageId: { type: Number, default: null },
})
const emit = defineEmits(['select'])

const nodeById = computed(() => {
  const map = {}
  for (const n of props.nodes || []) map[n.stage_id] = n
  return map
})

const GROUP_LABEL = {
  decision: '决策',
  step: '步骤',
  hitl: '人工交互',
  answer: '终答',
}
const GROUP_CLS = {
  decision: 'border-blue-200 bg-blue-50/60',
  step: 'border-slate-200 bg-white',
  hitl: 'border-amber-200 bg-amber-50/60',
  answer: 'border-emerald-200 bg-emerald-50/60',
}

const entries = computed(() =>
  (props.groups || []).map((g) => ({
    ...g,
    label: g.label || (g.kind === 'step' && g.step_index != null
      ? `步骤 ${g.step_index + 1}`
      : GROUP_LABEL[g.kind] || g.kind),
    items: (g.stage_ids || []).map((id, i) => nodeById.value[id] || {
      stage_id: id,
      node_name: g.node_names?.[i] || `stage #${id}`,
      status: g.status,
      latency_ms: null,
      attempt: 1,
      evidence_count: 0,
      citation_count: 0,
      context_flags: [],
    }),
  }))
)

function nodeTitle(n) {
  if (n.decision_summary) return n.decision_summary
  return n.node_name
}
</script>

<template>
  <div class="audit-timeline space-y-2">
    <p v-if="!entries.length" class="rounded border border-dashed bg-white p-6 text-center text-sm text-slate-400">
      该 run 暂无阶段节点
    </p>

    <template v-for="g in entries" :key="g.group_id">
      <details v-if="g.kind === 'step'"
               class="rounded-lg border p-2 text-xs"
               :class="GROUP_CLS.step"
               :open="g.error_count > 0">
        <summary class="flex cursor-pointer flex-wrap items-center gap-2">
          <span class="rounded bg-slate-100 px-1.5 py-0.5 font-medium text-slate-600">
            {{ g.label }}
          </span>
          <span class="text-slate-400">{{ g.items.length }} 节点</span>
          <span v-if="g.evidence_count" class="text-violet-600">证据 {{ g.evidence_count }}</span>
          <span v-if="g.error_count" class="text-red-500">错误 {{ g.error_count }}</span>
          <span class="ml-auto text-slate-400">{{ fmtMs(g.latency_ms) }}</span>
        </summary>
        <div class="mt-2 grid gap-1 md:grid-cols-2">
          <button v-for="n in g.items" :key="n.stage_id" type="button"
                  class="rounded border px-2 py-1 text-left hover:border-blue-300"
                  :class="n.stage_id === selectedStageId ? 'border-blue-400 bg-blue-50/50' : 'border-slate-200 bg-white'"
                  @click="emit('select', n.stage_id)">
            <span class="font-mono text-[11px] text-slate-600">{{ n.node_name }}</span>
            <span v-if="n.attempt > 1" class="ml-1 text-[10px] text-indigo-600">#{{ n.attempt }}</span>
            <span class="ml-1 text-[10px]"
                  :class="STATUS_CLS[n.status] || 'text-slate-400'">{{ n.status }}</span>
            <span class="ml-1 text-[10px] text-slate-400">{{ fmtMs(n.latency_ms) }}</span>
            <p class="truncate text-[11px] text-slate-500">{{ nodeTitle(n) }}</p>
          </button>
        </div>
      </details>

      <div v-else class="rounded-lg border p-2" :class="GROUP_CLS[g.kind]">
        <div class="flex flex-wrap items-center gap-2 text-xs">
          <span class="rounded bg-white px-1.5 py-0.5 font-medium text-slate-600">
            {{ GROUP_LABEL[g.kind] || g.label }}
          </span>
          <span class="text-slate-400">{{ fmtMs(g.latency_ms) }}</span>
          <span v-if="g.evidence_count" class="text-violet-600">证据 {{ g.evidence_count }}</span>
          <span v-if="g.error_count" class="text-red-500">错误 {{ g.error_count }}</span>
          <span class="ml-auto text-[10px] text-slate-400">第 {{ g.plan_round }} 轮</span>
        </div>
        <div class="mt-1 flex flex-col gap-1">
          <button v-for="n in g.items" :key="n.stage_id" type="button"
                  class="rounded border px-2 py-1 text-left hover:border-blue-300"
                  :class="n.stage_id === selectedStageId ? 'border-blue-400 bg-white' : 'border-slate-200 bg-white'"
                  @click="emit('select', n.stage_id)">
            <div class="flex flex-wrap items-center gap-2 text-xs">
              <span class="font-mono text-slate-600">{{ n.node_name }}</span>
              <span class="text-[10px]" :class="STATUS_CLS[n.status] || 'text-slate-400'">{{ n.status }}</span>
              <span class="text-[10px] text-slate-400">{{ fmtMs(n.latency_ms) }}</span>
              <span v-if="n.evidence_count" class="text-[10px] text-violet-600">证据 {{ n.evidence_count }}</span>
              <span v-if="n.citation_count" class="text-[10px] text-emerald-600">引用 {{ n.citation_count }}</span>
              <span v-if="n.error" class="text-[10px] text-red-500">{{ n.error }}</span>
            </div>
            <p class="truncate text-[11px] text-slate-500">{{ nodeTitle(n) }}</p>
            <p v-if="n.context_flags?.length" class="text-[10px] text-amber-600">
              上下文: {{ n.context_flags.join(' · ') }}
            </p>
          </button>
        </div>
      </div>
    </template>
  </div>
</template>
