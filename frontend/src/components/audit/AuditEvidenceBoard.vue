<!-- AuditEvidenceBoard.vue
  证据板: 工具调用 → 文档 → 终答引用 → 缺口。
  evidence_id 是 run 内稳定哈希, 点击可定位到来源 stage。
-->
<script setup>
import { computed } from 'vue'
import { fmtMs, KIND_LABEL } from './auditUi'

const props = defineProps({
  evidence: { type: Object, default: () => ({}) },
})
const emit = defineEmits(['locate'])

const tools = computed(() => props.evidence?.tool_calls || [])
const docs = computed(() => props.evidence?.documents || [])
const citations = computed(() => props.evidence?.citations || [])
const gaps = computed(() => props.evidence?.gaps || [])

const GAP_LABEL = {
  retrieved_not_used: '检索到但未进入分析',
  cited_without_source: '引用无对应来源文档',
  no_evidence: '无检索证据',
}

function usedText(v) {
  if (v === true) return '✓'
  if (v === false) return '—'
  return '?'
}

function usedTitle(v, kind) {
  if (v === true) return `已进入${kind}`
  if (v === false) return `未进入${kind}`
  return '旧 run 未采集, 未知'
}

function locateDoc(d) {
  if (d.source_stage_id == null) return
  emit('locate', { stageId: d.source_stage_id, evidenceId: d.evidence_id })
}

function locateGap(g) {
  if (g.stage_id == null) return
  emit('locate', { stageId: g.stage_id, evidenceId: g.evidence_id || null })
}

function countText(c) {
  const parts = []
  if (c?.laws) parts.push(`法条 ${c.laws}`)
  if (c?.cases) parts.push(`案例 ${c.cases}`)
  if (c?.memory) parts.push(`记忆 ${c.memory}`)
  if (c?.web) parts.push(`网络 ${c.web}`)
  return parts.join(' · ') || '无结果'
}
</script>

<template>
  <div class="audit-evidence-board space-y-2">
    <p v-if="!tools.length && !docs.length && !citations.length && !gaps.length"
       class="rounded border border-dashed bg-white p-6 text-center text-sm text-slate-400">
      本次 run 没有可关联的证据
    </p>

    <details v-if="tools.length" class="rounded-lg border bg-white p-2 text-xs" open>
      <summary class="cursor-pointer font-medium text-slate-600">
        工具调用({{ tools.length }})
      </summary>
      <div class="mt-1 overflow-x-auto">
        <table class="w-full text-left text-[11px]">
          <thead class="text-slate-400">
            <tr><th class="py-1">工具</th><th>状态</th><th>耗时</th><th>结果</th><th>阶段</th></tr>
          </thead>
          <tbody class="text-slate-600">
            <tr v-for="(t, i) in tools" :key="t.span_id || i" class="border-t">
              <td class="py-1 font-mono">{{ t.tool }}</td>
              <td :class="t.status === 'ok' ? 'text-emerald-600' : 'text-red-500'">{{ t.status || '—' }}</td>
              <td>{{ fmtMs(t.latency_ms) }}</td>
              <td class="text-slate-500">{{ countText(t.counts) }}</td>
              <td>
                <button v-if="t.stage_id" type="button" class="text-blue-600 hover:underline"
                        @click="emit('locate', { stageId: t.stage_id, evidenceId: null })">
                  #{{ t.stage_id }}
                </button>
                <span v-else class="text-slate-300">—</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </details>

    <details v-if="docs.length" class="rounded-lg border bg-white p-2 text-xs" open>
      <summary class="cursor-pointer font-medium text-slate-600">
        证据文档({{ docs.length }})
        <span v-if="evidence?.truncated" class="ml-1 text-[10px] text-amber-600">已截断</span>
      </summary>
      <div class="mt-1 overflow-x-auto">
        <table class="w-full text-left text-[11px]">
          <thead class="text-slate-400">
            <tr><th class="py-1">类型</th><th>标题</th><th>分数</th><th>分析</th><th>引用</th><th>阶段</th></tr>
          </thead>
          <tbody class="text-slate-600">
            <tr v-for="d in docs" :key="d.evidence_id" class="border-t">
              <td class="py-1">{{ KIND_LABEL[d.kind] || d.kind }}</td>
              <td class="max-w-80 truncate" :title="d.title">{{ d.title || d.evidence_id }}</td>
              <td>{{ d.score == null ? '—' : Number(d.score).toFixed(3) }}</td>
              <td :title="usedTitle(d.used_in_analysis, '分析')">{{ usedText(d.used_in_analysis) }}</td>
              <td :title="usedTitle(d.used_in_answer, '终答')">{{ usedText(d.used_in_answer) }}</td>
              <td>
                <button type="button" class="text-blue-600 hover:underline"
                        :disabled="d.source_stage_id == null"
                        @click="locateDoc(d)">#{{ d.source_stage_id ?? '—' }}</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </details>

    <details v-if="citations.length" class="rounded-lg border bg-white p-2 text-xs" open>
      <summary class="cursor-pointer font-medium text-slate-600">
        终答引用({{ citations.length }})
      </summary>
      <ul class="mt-1 space-y-0.5">
        <li v-for="(c, i) in citations" :key="i" class="flex items-start gap-2 text-[11px]">
          <span :class="c.resolved ? 'text-emerald-600' : 'text-amber-600'">
            {{ c.resolved ? '✓' : '!' }}
          </span>
          <span class="rounded bg-slate-100 px-1 text-[10px] text-slate-500">{{ KIND_LABEL[c.type] || c.type }}</span>
          <span class="flex-1 text-slate-600">{{ c.label }}</span>
          <span class="font-mono text-[10px] text-slate-400">
            {{ c.evidence_ids?.length ? c.evidence_ids[0] : '无 evidence_id' }}
          </span>
        </li>
      </ul>
    </details>

    <div v-if="gaps.length" class="rounded-lg border border-amber-200 bg-amber-50 p-2 text-xs">
      <p class="font-medium text-amber-800">证据缺口({{ gaps.length }})</p>
      <ul class="mt-1 space-y-0.5">
        <li v-for="(g, i) in gaps" :key="i" class="flex items-start gap-2 text-[11px] text-amber-800">
          <span class="rounded bg-white/70 px-1 text-[10px]">{{ GAP_LABEL[g.code] || g.code }}</span>
          <span class="flex-1">{{ g.label || g.evidence_id || '—' }}</span>
          <button v-if="g.stage_id" type="button" class="text-blue-600 hover:underline"
                  @click="locateGap(g)">#{{ g.stage_id }}</button>
        </li>
      </ul>
    </div>
  </div>
</template>
