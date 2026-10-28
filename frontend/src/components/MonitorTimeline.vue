<!-- MonitorTimeline.vue
  垂直时间轴(D-spec §六): 总占比条 → 按 run 分组的节点卡序列;
  HITL 续跑之间用分隔条展示用户回答; 卡内展示耗时占执行比例。
  无有效内容的行不渲染, 避免时间轴出现空行。
-->
<script setup>
import { computed } from 'vue'
import MinorStateCard from './MinorStateCard.vue'
import TimelineCompositionBar from './TimelineCompositionBar.vue'

const props = defineProps({
  timeline: { type: Object, default: null },
  selectedStageId: { type: Number, default: null },
  phaseFilter: { type: String, default: '' },
  nodeFilter: { type: String, default: '' },
})
const emit = defineEmits(['select'])

const runsById = computed(() => {
  const map = {}
  for (const r of props.timeline?.runs || []) map[r.run_id] = r
  return map
})

function matches(state) {
  if (!state || !state.node_name) return false
  if (props.phaseFilter && state.phase !== props.phaseFilter) return false
  if (props.nodeFilter && state.node_name !== props.nodeFilter) return false
  return true
}

const entries = computed(() => {
  const items = (props.timeline?.items || []).filter(
    (x) => x.kind !== 'node' || matches(x.state)
  )
  // 分隔条后若没有可见节点, 一并丢弃(过滤后不留孤立分隔)
  const kept = []
  for (let i = 0; i < items.length; i += 1) {
    const it = items[i]
    if (it.kind === 'divider') {
      const hasNext = items
        .slice(i + 1)
        .some((n) => n.kind === 'node' && n.run_id === it.run_id)
      if (!hasNext) continue
    }
    kept.push(it)
  }

  const out = []
  let lastRun = null
  for (const it of kept) {
    if (it.kind === 'node') {
      if (it.run_id !== lastRun) {
        out.push({ kind: 'run', run_id: it.run_id, run: runsById.value[it.run_id] })
        lastRun = it.run_id
      }
      out.push(it)
    } else {
      out.push(it)
    }
  }
  return out
})

const total = computed(() => props.timeline?.total_composition || null)
const totalNodes = computed(
  () => (props.timeline?.items || []).filter((x) => x.kind === 'node').length
)

function fmtDuration(ms) {
  const n = Number(ms || 0)
  if (n < 1000) return `${n}ms`
  if (n < 60000) return `${(n / 1000).toFixed(1)}s`
  return `${(n / 60000).toFixed(1)}m`
}
</script>

<template>
  <div id="monitor-timeline" class="monitor-timeline space-y-3">
    <div v-if="total" id="monitor-timeline-total"
         class="rounded-lg border border-slate-200 bg-white p-3">
      <div class="mb-2 flex items-center justify-between">
        <h3 class="text-sm font-semibold text-slate-600">链总时长构成</h3>
        <span class="text-xs text-slate-400">{{ totalNodes }} 个节点</span>
      </div>
      <TimelineCompositionBar :composition="total" />
    </div>

    <div v-if="entries.length" id="monitor-timeline-stream"
         class="relative space-y-3 pl-6">
      <span class="pointer-events-none absolute bottom-3 left-2 top-3 w-px bg-slate-200" />
      <template v-for="(e, idx) in entries" :key="e.kind === 'node' ? `n-${e.state.stage_id}` : `${e.kind}-${idx}`">
        <div v-if="e.kind === 'run'" :id="`monitor-timeline-run-${e.run_id}`"
             class="monitor-run-group relative -ml-6 flex flex-wrap items-center gap-2 rounded-md bg-slate-200/60 px-2 py-1 text-xs text-slate-600">
          <span class="font-mono">{{ e.run?.run_id?.slice(0, 18) || e.run_id.slice(0, 18) }}…</span>
          <span class="rounded bg-white px-1.5 py-0.5">{{ e.run?.status || '—' }}</span>
          <span v-if="e.run?.parent_run_id" class="rounded bg-indigo-50 px-1.5 py-0.5 text-indigo-600">
            续跑自 {{ e.run.parent_run_id.slice(0, 12) }}…
          </span>
          <span class="ml-auto text-slate-500">
            {{ fmtDuration(e.run?.composition?.wall_clock_ms) }} ·
            执行 {{ fmtDuration(e.run?.composition?.execution_ms) }}
          </span>
        </div>

        <div v-else-if="e.kind === 'divider'" :id="`monitor-timeline-divider-${idx}`"
             class="monitor-divider relative -ml-1 flex items-center gap-2 py-1">
          <span class="h-2 w-2 rounded-full bg-amber-400" />
          <span class="rounded-full border border-amber-200 bg-amber-50 px-3 py-1 text-xs text-amber-700">
            {{ e.text || '用户回答后继续' }}
          </span>
        </div>

        <div v-else class="monitor-timeline-row">
          <MinorStateCard
            :state="e.state"
            :percent-of-run="e.percent_of_run"
            :percent-of-execution="e.percent_of_execution"
            :selected="selectedStageId != null && e.state.stage_id === selectedStageId"
            @select="emit('select', $event)"
          />
        </div>
      </template>
    </div>

    <p v-else id="monitor-timeline-empty" class="rounded border border-dashed bg-white p-6 text-center text-sm text-slate-400">
      该 run 暂无节点内容
    </p>
  </div>
</template>
