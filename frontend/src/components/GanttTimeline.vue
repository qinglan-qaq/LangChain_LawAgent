<!-- GanttTimeline.vue
  甘特时间线(D6): 共享时间轴, 节点行×seq 叠行, CSS 自绘(不引图表库)。
  条色: ok 绿 / error 红 / interrupted 黄 / cancelled 灰 / running 蓝延伸至现在。
  tools 行标注该次执行窗内实际调用的工具名(span_type=tool 的 spans)。
-->
<script setup>
import { computed } from 'vue'
import { fmtCSTTime } from '../lib/time'

const props = defineProps({
  stages: { type: Array, default: () => [] }, // MonitorStage[]
  spans: { type: Array, default: () => [] }, // MonitorSpan[](tool 名标注用)
  now: { type: Number, default: () => Date.now() },
})
const emit = defineEmits(['select'])

const t0t1 = computed(() => {
  const pts = props.stages.flatMap((s) =>
    [Date.parse(s.started_at), s.ended_at ? Date.parse(s.ended_at) : props.now]
  )
  if (!pts.length) return [0, 1]
  return [Math.min(...pts), Math.max(...pts, Math.min(...pts) + 1)]
})

function pct(iso) {
  const [a, b] = t0t1.value
  return (100 * (Date.parse(iso) - a)) / (b - a)
}

function bar(s) {
  const l = pct(s.started_at)
  const r = s.ended_at ? pct(s.ended_at) : 100
  return { left: `${l}%`, width: `${Math.max(r - l, 0.5)}%` }
}

const CLS = {
  ok: 'bg-emerald-500/80', error: 'bg-red-500/80',
  interrupted: 'bg-amber-500/80', cancelled: 'bg-slate-400/70',
  running: 'bg-blue-500/80 animate-pulse',
}

// 阶段执行窗内的 tool span 名列表(tools 行标注 + title 全量)
function toolsOf(s) {
  if (!props.spans.length) return []
  const t0 = Date.parse(s.started_at)
  const t1 = s.ended_at ? Date.parse(s.ended_at) : props.now
  const names = []
  for (const sp of props.spans) {
    if (sp.span_type !== 'tool' || !sp.started_at) continue
    const ts = Date.parse(sp.started_at)
    if (!isNaN(ts) && ts >= t0 - 1 && ts <= t1 + 1 && !names.includes(sp.name)) {
      names.push(sp.name)
    }
  }
  return names
}

const rows = computed(() =>
  [...props.stages].sort((a, b) =>
    Date.parse(a.started_at) - Date.parse(b.started_at))
    .map((s) => ({ stage: s, tools: toolsOf(s) }))
)
</script>

<template>
  <div id="gantt-timeline" class="gantt-timeline border rounded bg-white p-3">
    <div id="gantt-time-axis" class="gantt-time-axis text-xs text-slate-400 mb-2 flex justify-between">
      <span>{{ fmtCSTTime(new Date(t0t1[0]).toISOString()) }}</span>
      <span>{{ fmtCSTTime(new Date(t0t1[1]).toISOString()) }}</span>
    </div>
    <div id="gantt-rows" class="gantt-rows space-y-1">
      <div
        v-for="r in rows"
        :key="r.stage.node_name + ':' + r.stage.seq"
        :id="`gantt-row-${r.stage.node_name}-${r.stage.seq}`"
        class="gantt-row relative h-7 group cursor-pointer"
        @click="emit('select', r.stage)"
      >
        <span class="gantt-row-label absolute left-0 w-44 truncate text-xs text-slate-600 z-10 bg-white/60"
              :title="r.tools.length ? `${r.stage.node_name}: ${r.tools.join(', ')}` : r.stage.node_name">
          {{ r.stage.node_name }}<template v-if="r.stage.seq > 1"> (第{{ r.stage.seq }}次)</template>
          <span v-if="r.tools.length" class="text-slate-400"> · {{ r.tools.join(' + ') }}</span>
        </span>
        <div
          :id="`gantt-bar-${r.stage.node_name}-${r.stage.seq}`"
          class="gantt-bar absolute top-1 h-5 rounded opacity-90 hover:opacity-100 transition-opacity"
          :class="CLS[r.stage.status] || CLS.running"
          :style="bar(r.stage)"
          :title="`${r.stage.node_name} #${r.stage.seq} · ${r.stage.status} · ${r.stage.latency_ms ?? '…'}ms${r.tools.length ? ` · 工具: ${r.tools.join(', ')}` : ''}`"
        />
      </div>
    </div>
  </div>
</template>
