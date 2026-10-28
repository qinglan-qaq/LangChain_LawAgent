<!-- TimelineCompositionBar.vue
  时长构成条(D-spec §六): 执行 / 等待人工 / 其他开销 三段比例,
  颜色与图例同源, 悬停显示毫秒与百分比。纯 CSS, 无图表库。
-->
<script setup>
import { computed } from 'vue'

const props = defineProps({
  composition: { type: Object, default: null },
  compact: { type: Boolean, default: false },
})

const parts = computed(() => {
  const c = props.composition || {}
  const wall = Number(c.wall_clock_ms || 0)
  const segs = [
    {
      key: 'execution',
      label: '执行',
      ms: Number(c.execution_ms || 0),
      pct: Number(c.execution_pct || 0),
      cls: 'bg-blue-500',
      text: 'text-blue-600',
    },
    {
      key: 'wait',
      label: '等待人工',
      ms: Number(c.wait_human_ms || 0),
      pct: Number(c.wait_pct || 0),
      cls: 'bg-amber-500',
      text: 'text-amber-600',
    },
    {
      key: 'overhead',
      label: '其他开销',
      ms: Number(c.overhead_ms || 0),
      pct: Number(c.overhead_pct || 0),
      cls: 'bg-slate-400',
      text: 'text-slate-500',
    },
  ]
  // 无 wall_clock 时按毫秒占比兜底, 保证条不空
  if (!wall) {
    const sum = segs.reduce((a, b) => a + b.ms, 0) || 1
    for (const s of segs) s.pct = (s.ms / sum) * 100
  }
  return segs
})

function fmt(ms) {
  const n = Number(ms || 0)
  if (n < 1000) return `${n}ms`
  if (n < 60000) return `${(n / 1000).toFixed(1)}s`
  return `${(n / 60000).toFixed(1)}m`
}
</script>

<template>
  <div id="timeline-composition" class="timeline-composition" :class="compact ? 'space-y-1' : 'space-y-2'">
    <div class="flex items-center justify-between text-xs">
      <span class="text-slate-500">总时长 {{ fmt(composition?.wall_clock_ms) }}</span>
      <span class="text-slate-400">
        执行 {{ fmt(composition?.execution_ms) }} · 等待 {{ fmt(composition?.wait_human_ms) }}
      </span>
    </div>
    <div id="timeline-composition-bar"
         class="timeline-composition-bar flex h-3 w-full overflow-hidden rounded-full bg-slate-100"
         :class="compact ? 'h-2' : 'h-3'">
      <div v-for="s in parts" :key="s.key"
           :id="`timeline-composition-seg-${s.key}`"
           class="h-full transition-all duration-300"
           :class="s.cls"
           :style="{ width: `${Math.max(0, Math.min(100, s.pct))}%` }"
           :title="`${s.label}: ${fmt(s.ms)} (${s.pct.toFixed(1)}%)`" />
    </div>
    <div v-if="!compact" id="timeline-composition-legend"
         class="timeline-composition-legend flex flex-wrap gap-x-4 gap-y-1 text-xs">
      <span v-for="s in parts" :key="s.key" class="inline-flex items-center gap-1" :class="s.text">
        <span class="inline-block h-2 w-2 rounded-full" :class="s.cls" />
        {{ s.label }} {{ s.pct.toFixed(1) }}%
      </span>
    </div>
  </div>
</template>
