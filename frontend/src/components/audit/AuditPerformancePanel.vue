<!-- AuditPerformancePanel.vue
  性能板: 总耗时构成 / 慢点 / 错误重试 / tokens / 触顶。
-->
<script setup>
import { computed } from 'vue'
import { fmtMs, fmtNum } from './auditUi'

const props = defineProps({
  performance: { type: Object, default: () => ({}) },
})
const emit = defineEmits(['select'])

const p = computed(() => props.performance || {})
const tokens = computed(() => p.value.tokens || {})
</script>

<template>
  <div class="audit-performance-panel space-y-2">
    <div class="grid grid-cols-2 gap-2 text-xs md:grid-cols-4">
      <div class="rounded border bg-white p-2">
        <p class="text-slate-400">总墙钟</p>
        <p class="text-lg font-semibold text-slate-700">{{ fmtMs(p.total_ms) }}</p>
      </div>
      <div class="rounded border bg-white p-2">
        <p class="text-slate-400">节点执行</p>
        <p class="text-lg font-semibold text-blue-600">{{ fmtMs(p.execution_ms) }}</p>
      </div>
      <div class="rounded border bg-white p-2">
        <p class="text-slate-400">人工等待</p>
        <p class="text-lg font-semibold text-amber-600">{{ fmtMs(p.wait_human_ms) }}</p>
      </div>
      <div class="rounded border bg-white p-2">
        <p class="text-slate-400">框架/其他</p>
        <p class="text-lg font-semibold text-slate-500">{{ fmtMs(p.overhead_ms) }}</p>
      </div>
    </div>

    <div class="grid gap-2 md:grid-cols-2">
      <div class="rounded-lg border bg-white p-2 text-xs">
        <p class="mb-1 font-medium text-slate-600">最慢节点</p>
        <button v-for="s in p.slowest || []" :key="s.stage_id" type="button"
                class="flex w-full items-center gap-2 rounded px-1 py-0.5 text-left hover:bg-slate-50"
                @click="emit('select', s.stage_id)">
          <span class="w-36 truncate font-mono text-[11px] text-slate-600">{{ s.node_name }}</span>
          <span class="text-[11px] text-slate-500">{{ fmtMs(s.latency_ms) }}</span>
          <span class="ml-auto text-[10px] text-slate-400">{{ s.pct_of_execution }}%</span>
        </button>
        <p v-if="!(p.slowest || []).length" class="text-slate-400">无</p>
      </div>

      <div class="rounded-lg border bg-white p-2 text-xs">
        <p class="mb-1 font-medium text-slate-600">tokens / 重试</p>
        <p class="text-slate-600">
          合计 <span class="font-semibold">{{ fmtNum(tokens.total) }}</span>
          · prompt {{ fmtNum(tokens.prompt) }} · completion {{ fmtNum(tokens.completion) }}
        </p>
        <p class="mt-1 text-slate-500">重试节点 {{ p.retries ?? 0 }}</p>
        <p class="mt-0.5" :class="p.limit_hit ? 'text-red-500' : 'text-slate-400'">
          {{ p.limit_hit ? '触顶运行(limit_hit)' : '未触顶' }}
        </p>
      </div>
    </div>

    <div v-if="(p.errors || []).length" class="rounded-lg border border-red-200 bg-red-50 p-2 text-xs">
      <p class="mb-1 font-medium text-red-700">错误({{ p.errors.length }})</p>
      <button v-for="(e, i) in p.errors" :key="i" type="button"
              class="flex w-full items-start gap-2 rounded px-1 py-0.5 text-left hover:bg-white/60"
              @click="e.stage_id && emit('select', e.stage_id)">
        <span class="w-36 truncate font-mono text-[11px] text-red-700">{{ e.node_name || '—' }}</span>
        <span class="flex-1 text-[11px] text-red-600">{{ e.error }}</span>
        <span v-if="e.retry" class="text-[10px] text-red-500">retry {{ e.retry }}</span>
      </button>
    </div>
  </div>
</template>
