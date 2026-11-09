<!-- AuditRunHeader.vue
  run 头部: 身份/状态/规模 + HITL 链标签(含人工等待时长)。
  只展示, 不发请求。
-->
<script setup>
import { fmtMs, shortId, STATUS_CLS } from './auditUi'

defineProps({
  run: { type: Object, default: null },
  chain: { type: Array, default: () => [] },
  degraded: { type: Boolean, default: false },
})
</script>

<template>
  <header class="audit-run-header rounded-lg border border-slate-200 bg-white p-3">
    <div class="flex flex-wrap items-center gap-2">
      <h2 class="text-base font-semibold text-slate-700">统一审计</h2>
      <span class="rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-500">
        {{ run?.mode || run?.run_type || '—' }}
      </span>
      <span class="rounded bg-slate-50 px-1.5 py-0.5 text-xs"
            :class="STATUS_CLS[run?.status] || 'text-slate-500'">
        {{ run?.status || '—' }}
      </span>
      <span v-if="degraded"
            class="rounded border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-xs text-amber-700"
            title="旧 run 缺少证据/上下文/tokens 采集, 部分结论为 unknown">
        部分可审计
      </span>
      <span class="ml-auto font-mono text-[11px] text-slate-400" :title="run?.run_id">
        {{ shortId(run?.run_id) }}…
      </span>
    </div>

    <p class="mt-1 truncate text-sm text-slate-600" :title="run?.query">{{ run?.query || '(无提问)' }}</p>

    <div class="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-slate-400">
      <span>会话 {{ shortId(run?.session_id) }}</span>
      <span>{{ run?.stage_count ?? 0 }} 阶段 · {{ run?.minor_count ?? 0 }} 内容行</span>
      <span>总耗时 {{ fmtMs(run?.latency_ms) }}</span>
      <span>终答 {{ run?.answer_len ?? 0 }} 字</span>
      <span v-if="run?.parent_run_id">续跑自 {{ shortId(run.parent_run_id) }}</span>
      <span v-if="run?.current_node">当前 {{ run.current_node }}</span>
    </div>

    <div v-if="chain.length > 1" class="mt-2 flex flex-wrap items-center gap-2 text-[11px]">
      <template v-for="(r, i) in chain" :key="r.run_id">
        <span v-if="i > 0" class="text-amber-600">
          ↳ 人工等待 {{ fmtMs(r.wait_before_ms) }}
        </span>
        <span class="rounded border px-1.5 py-0.5"
              :class="r.run_id === run?.run_id ? 'border-blue-300 bg-blue-50 text-blue-700' : 'bg-white text-slate-500'">
          {{ i + 1 }}. {{ shortId(r.run_id) }} · {{ r.status }} · {{ r.stage_count }} 阶段
        </span>
      </template>
    </div>
  </header>
</template>
