<!-- MinorStateCard.vue
  时间轴节点卡: 节点名/轮次/相位/耗时 + 占本次执行比例条 + 内容摘要。
  只渲染有效行(调用方过滤空 node_name); 点击交给父层加载阶段详情。
-->
<script setup>
import { computed } from 'vue'
import {
  TooltipContent, TooltipProvider, TooltipRoot, TooltipTrigger,
} from 'reka-ui'
import { fmtCSTTime } from '../lib/time'

const props = defineProps({
  state: { type: Object, required: true },
  percentOfRun: { type: Number, default: null },
  percentOfExecution: { type: Number, default: null },
  selected: { type: Boolean, default: false },
})
const emit = defineEmits(['select'])

const HITL_NODES = new Set([
  'ask_element', 'element_fill', 'mid_clarify', 'hitl_degrade', 'hitl_budget',
  'risk_confirm', 'clarify', 'pdf_confirm', 'field_clarify', 'docx_confirm',
  'degrade_confirm', 'budget_confirm',
])
const RETRIEVAL_NODES = new Set(['tools', 'merge'])

const category = computed(() => {
  const n = props.state.node_name
  if (HITL_NODES.has(n) || props.state.phase === 'waiting_human') return 'hitl'
  if (RETRIEVAL_NODES.has(n)) return 'retrieval'
  if (['finalize', 'chitchat'].includes(n)) return 'answer'
  return 'reasoning'
})

const CAT = {
  hitl: {
    label: '人工交互',
    dot: 'bg-amber-500 ring-amber-100',
    chip: 'bg-amber-50 text-amber-700 border-amber-200',
    bar: 'bg-amber-500',
  },
  retrieval: {
    label: '检索',
    dot: 'bg-violet-500 ring-violet-100',
    chip: 'bg-violet-50 text-violet-700 border-violet-200',
    bar: 'bg-violet-500',
  },
  answer: {
    label: '终答',
    dot: 'bg-emerald-500 ring-emerald-100',
    chip: 'bg-emerald-50 text-emerald-700 border-emerald-200',
    bar: 'bg-emerald-500',
  },
  reasoning: {
    label: '推理',
    dot: 'bg-blue-500 ring-blue-100',
    chip: 'bg-blue-50 text-blue-700 border-blue-200',
    bar: 'bg-blue-500',
  },
}

const meta = computed(() => CAT[category.value])
const isRunning = computed(() => props.state.phase === 'running')
const isWaiting = computed(() => props.state.phase === 'waiting_human')

const phaseText = computed(() => {
  if (isWaiting.value) return '等待人工'
  if (isRunning.value) return '执行中'
  if (props.state.error) return '异常'
  return '完成'
})
const phaseCls = computed(() => {
  if (isWaiting.value) return 'bg-amber-100 text-amber-700'
  if (isRunning.value) return 'bg-blue-100 text-blue-700 animate-pulse'
  if (props.state.error) return 'bg-red-100 text-red-700'
  return 'bg-slate-100 text-slate-500'
})

const preview = computed(() => {
  const out = props.state.output_text || ''
  const q = props.state.output_json?.question || ''
  const text = out || q
  return text.length > 240 ? `${text.slice(0, 240)}…` : text
})

const truncated = computed(() =>
  Boolean(props.state.refs?.truncated_chars || props.state.refs?.json_truncated)
)
const placeholder = computed(() => props.state.content_source === 'fallback_placeholder')

const pctWidth = computed(() => {
  const v = Number(props.percentOfExecution ?? 0)
  return Math.max(0, Math.min(100, v))
})

function fmtMs(ms) {
  if (ms == null) return '—'
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(2)}s`
}
</script>

<template>
  <TooltipProvider>
    <button
      :id="`minor-card-${state.stage_id}`"
      type="button"
      class="minor-card group relative w-full rounded-lg border bg-white p-3 text-left transition-shadow hover:shadow-md focus:outline-none focus:ring-2 focus:ring-blue-200"
      :class="selected ? 'border-blue-400 ring-1 ring-blue-300' : 'border-slate-200'"
      @click="emit('select', state.stage_id)"
    >
      <span class="absolute -left-5 top-4 inline-block h-2.5 w-2.5 rounded-full ring-4"
            :class="meta.dot" />
      <div class="flex items-start gap-3">
        <div class="min-w-0 flex-1">
        <div class="flex flex-wrap items-center gap-2">
          <span class="text-sm font-semibold text-slate-700">{{ state.node_name }}</span>
          <span v-if="state.seq > 1" class="text-xs text-slate-400">第 {{ state.seq }} 次</span>
          <span v-if="state.attempt > 1" class="rounded bg-indigo-50 px-1.5 py-0.5 text-[11px] text-indigo-600">
            续跑 #{{ state.attempt }}
          </span>
          <span class="rounded px-1.5 py-0.5 text-[11px]" :class="phaseCls">{{ phaseText }}</span>
          <span class="rounded border px-1.5 py-0.5 text-[11px]" :class="meta.chip">{{ meta.label }}</span>
          <span v-if="state.route" class="rounded bg-slate-50 px-1.5 py-0.5 text-[11px] text-slate-500">
            → {{ state.route }}
          </span>
          <TooltipRoot v-if="placeholder">
            <TooltipTrigger as-child>
              <span class="rounded bg-orange-50 px-1.5 py-0.5 text-[11px] text-orange-600">占位</span>
            </TooltipTrigger>
            <TooltipContent class="rounded bg-slate-800 px-2 py-1 text-xs text-white shadow">
              minor_states 行缺失, 后端按 stage_chain 合成占位内容
            </TooltipContent>
          </TooltipRoot>
          <TooltipRoot v-if="truncated">
            <TooltipTrigger as-child>
              <span class="rounded bg-slate-50 px-1.5 py-0.5 text-[11px] text-slate-400">已截断</span>
            </TooltipTrigger>
            <TooltipContent class="rounded bg-slate-800 px-2 py-1 text-xs text-white shadow">
              文本或 JSON 超过投影上限, 原文见阶段详情 refs
            </TooltipContent>
          </TooltipRoot>
        </div>

        <div class="mt-1 flex flex-wrap items-center gap-x-3 text-[11px] text-slate-400">
          <span :title="`占本 run 墙钟 ${percentOfRun ?? '—'}%`">
            耗时 {{ fmtMs(state.latency_ms) }}
          </span>
          <span>开始 {{ fmtCSTTime(state.started_at) || '—' }}</span>
          <span v-if="state.metrics?.tokens">
            tokens {{ state.metrics.tokens }}
          </span>
        </div>

        <p v-if="preview" class="mt-1 line-clamp-2 text-xs leading-relaxed text-slate-600">
          {{ preview }}
        </p>
        <p v-else-if="!isRunning" class="mt-1 text-xs text-slate-300">无文本输出</p>

        <div v-if="percentOfExecution != null" class="mt-2 flex items-center gap-2">
          <div class="h-1.5 flex-1 overflow-hidden rounded-full bg-slate-100">
            <div class="h-full rounded-full transition-all duration-300"
                 :class="meta.bar" :style="{ width: `${pctWidth}%` }" />
          </div>
          <span class="w-12 text-right text-[11px] text-slate-400">
            {{ Number(percentOfExecution).toFixed(1) }}%
          </span>
        </div>
      </div>
    </div>
    </button>
  </TooltipProvider>
</template>
