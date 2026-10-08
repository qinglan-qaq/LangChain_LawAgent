<!-- GanttTimeline.vue
  甘特时间线(D6): 共享时间轴, 节点行×seq 叠行, CSS 自绘(不引图表库)。
  条色: ok 绿 / error 红 / interrupted 黄 / cancelled 灰 / running 蓝延伸至现在。
-->
<script setup>
import { computed } from 'vue'

const props = defineProps({
  stages: { type: Array, default: () => [] }, // MonitorStage[]
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
const rows = computed(() =>
  [...props.stages].sort((a, b) =>
    Date.parse(a.started_at) - Date.parse(b.started_at))
)
</script>

<template>
  <div class="border rounded bg-white p-3">
    <div class="text-xs text-slate-400 mb-2 flex justify-between">
      <span>{{ new Date(t0t1[0]).toLocaleTimeString() }}</span>
      <span>{{ new Date(t0t1[1]).toLocaleTimeString() }}</span>
    </div>
    <div class="space-y-1">
      <div
        v-for="s in rows"
        :key="s.node_name + ':' + s.seq"
        class="relative h-7 group cursor-pointer"
        @click="emit('select', s)"
      >
        <span class="absolute left-0 w-44 truncate text-xs text-slate-600 z-10 bg-white/60">
          {{ s.node_name }}<template v-if="s.seq > 1"> (第{{ s.seq }}次)</template>
        </span>
        <div
          class="absolute top-1 h-5 rounded opacity-90 hover:opacity-100 transition-opacity"
          :class="CLS[s.status] || CLS.running"
          :style="bar(s)"
          :title="`${s.node_name} #${s.seq} · ${s.status} · ${s.latency_ms ?? '…'}ms`"
        />
      </div>
    </div>
  </div>
</template>
