<!-- AuditNodeDrawer.vue
  单节点审计抽屉: 概览/决策/证据/上下文/调用/原始数据。
  原始数据 tab 默认不加载, 点击才 emit load-raw(可能含案情与内部 prompt)。
-->
<script setup>
import { computed } from 'vue'
import { fmtMs, KIND_LABEL, STATUS_CLS } from './auditUi'

const props = defineProps({
  detail: { type: Object, default: null },
  loading: { type: Boolean, default: false },
  rawLoading: { type: Boolean, default: false },
  tab: { type: String, default: 'overview' },
})
const emit = defineEmits(['close', 'load-raw', 'update:tab'])

const TABS = [
  ['overview', '概览'],
  ['decision', '决策'],
  ['evidence', '证据'],
  ['context', '上下文'],
  ['calls', '调用'],
  ['raw', '原始数据'],
]

const node = computed(() => props.detail?.node || null)
const decision = computed(() => props.detail?.decision || {})
const evidence = computed(() => props.detail?.evidence || {})
const context = computed(() => props.detail?.context || {})
const calls = computed(() => props.detail?.calls || [])

const SEG_LABEL = {
  task: '任务', current_query: '提问', critical_facts: '要素', history: '历史',
  evidence: '证据', constraints: '约束', memory: '记忆',
}

function setTab(k) {
  emit('update:tab', k)
  if (k === 'raw' && !props.detail?.raw) emit('load-raw')
}

function rawText() {
  try {
    return JSON.stringify(props.detail?.raw || {}, null, 2)
  } catch {
    return String(props.detail?.raw)
  }
}
</script>

<template>
  <div class="fixed inset-0 z-40 flex justify-end">
    <div class="absolute inset-0 bg-slate-900/30" @click="emit('close')" />
    <aside class="relative flex h-full w-full max-w-2xl flex-col border-l bg-white shadow-xl">
      <header class="flex items-start gap-2 border-b p-3">
        <div class="min-w-0">
          <h3 class="truncate text-base font-semibold text-slate-700">
            {{ node?.node_name || '节点详情' }}
            <span v-if="node?.attempt > 1" class="text-xs font-normal text-indigo-600">#{{ node.attempt }}</span>
          </h3>
          <p class="text-[11px] text-slate-400">
            stage #{{ node?.stage_id }} · {{ node?.status }} · {{ fmtMs(node?.latency_ms) }}
            <span v-if="node?.plan_round"> · 第 {{ node.plan_round }} 轮</span>
            <span v-if="node?.step_index != null"> · 步骤 {{ node.step_index + 1 }}</span>
          </p>
        </div>
        <button type="button" class="ml-auto rounded border px-2 py-0.5 text-sm text-slate-500 hover:text-slate-700"
                @click="emit('close')">✕</button>
      </header>

      <nav class="flex flex-wrap gap-1 border-b px-3 py-2 text-xs">
        <button v-for="[k, label] in TABS" :key="k" type="button"
                class="rounded px-2 py-1"
                :class="tab === k ? 'bg-blue-600 text-white' : 'bg-slate-100 text-slate-600 hover:bg-slate-200'"
                @click="setTab(k)">{{ label }}</button>
      </nav>

      <div class="min-h-0 flex-1 overflow-y-auto p-3 text-xs">
        <div v-if="loading" class="py-10 text-center text-sm text-slate-400">加载节点详情…</div>

        <template v-else-if="detail">
          <section v-if="tab === 'overview'" class="space-y-2">
            <div class="flex flex-wrap gap-2">
              <span class="rounded bg-slate-100 px-1.5 py-0.5">{{ node?.phase }}</span>
              <span class="rounded bg-slate-50 px-1.5 py-0.5" :class="STATUS_CLS[node?.status]">{{ node?.status }}</span>
              <span v-if="node?.route" class="rounded bg-blue-50 px-1.5 py-0.5 text-blue-700">
                → {{ node.route }}<template v-if="node.route_reason">({{ node.route_reason }})</template>
              </span>
              <span v-if="node?.decision_summary" class="rounded bg-violet-50 px-1.5 py-0.5 text-violet-700">
                {{ node.decision_summary }}
              </span>
            </div>
            <p v-if="node?.error" class="rounded border border-red-200 bg-red-50 p-2 text-red-700">
              {{ node.error }}
            </p>
            <div class="rounded border bg-slate-50 p-2 text-slate-600">
              <p>证据 {{ node?.evidence_count ?? 0 }} · 引用 {{ node?.citation_count ?? 0 }}</p>
              <p v-if="node?.context_flags?.length" class="mt-1 text-amber-700">
                上下文标记: {{ node.context_flags.join(' · ') }}
              </p>
              <p class="mt-1 text-slate-400">开始 {{ node?.started_at || '—' }} · 结束 {{ node?.ended_at || '—' }}</p>
            </div>
            <p v-if="decision.summary" class="rounded border bg-white p-2">
              <span class="text-slate-400">决策: </span>{{ decision.summary }}
              <span v-if="decision.reason" class="text-slate-500">({{ decision.reason }})</span>
            </p>
          </section>

          <section v-else-if="tab === 'decision'" class="space-y-2">
            <p><span class="text-slate-400">摘要: </span>{{ decision.summary || '—' }}</p>
            <p><span class="text-slate-400">理由: </span>{{ decision.reason || '—' }}</p>
            <p v-if="decision.tool"><span class="text-slate-400">工具: </span>{{ decision.tool }}</p>
            <p v-if="decision.hitl"><span class="text-slate-400">人工问题: </span>{{ decision.hitl }}</p>
            <div v-if="decision.steps?.length" class="rounded border bg-slate-50 p-2">
              <p class="mb-1 font-medium text-slate-600">计划步骤</p>
              <ol class="list-decimal space-y-1 pl-4">
                <li v-for="(s, i) in decision.steps" :key="i">
                  <span class="font-mono text-[11px] text-blue-700">{{ s.tool || '无工具' }}</span>
                  <span class="text-slate-600"> {{ s.description || '' }}</span>
                </li>
              </ol>
            </div>
          </section>

          <section v-else-if="tab === 'evidence'" class="space-y-2">
            <div>
              <p class="mb-1 font-medium text-slate-600">工具调用({{ evidence.tool_calls?.length || 0 }})</p>
              <p v-for="(t, i) in evidence.tool_calls || []" :key="i" class="text-slate-600">
                <span class="font-mono">{{ t.tool }}</span>
                <span class="ml-1" :class="STATUS_CLS[t.status]">{{ t.status }}</span>
                <span class="ml-1 text-slate-400">{{ fmtMs(t.latency_ms) }}</span>
              </p>
            </div>
            <div>
              <p class="mb-1 font-medium text-slate-600">文档({{ evidence.documents?.length || 0 }})</p>
              <p v-for="d in evidence.documents || []" :key="d.evidence_id" class="truncate text-slate-600">
                [{{ KIND_LABEL[d.kind] || d.kind }}] {{ d.title || d.evidence_id }}
                <span class="text-slate-400">{{ d.score == null ? '' : Number(d.score).toFixed(3) }}</span>
              </p>
            </div>
            <div v-if="evidence.citations?.length">
              <p class="mb-1 font-medium text-slate-600">引用({{ evidence.citations.length }})</p>
              <p v-for="(c, i) in evidence.citations" :key="i" class="text-slate-600">
                <span :class="c.resolved ? 'text-emerald-600' : 'text-amber-600'">{{ c.resolved ? '✓' : '!' }}</span>
                {{ c.label }}
              </p>
            </div>
            <div v-if="evidence.gaps?.length" class="rounded border border-amber-200 bg-amber-50 p-2 text-amber-800">
              <p class="font-medium">缺口</p>
              <p v-for="(g, i) in evidence.gaps" :key="i">{{ g.code }} · {{ g.label || g.evidence_id }}</p>
            </div>
          </section>

          <section v-else-if="tab === 'context'" class="space-y-2">
            <p class="text-slate-500">
              总注入 {{ context.segments ? Object.values(context.segments).reduce((a, s) => a + (s.present && s.injected ? s.chars : 0), 0) : 0 }} 字符
              <span v-if="context.inherited_from_stage_id" class="ml-2 text-amber-600">
                ↳ 继承自 #{{ context.inherited_from_stage_id }}
              </span>
            </p>
            <table class="w-full text-left text-[11px]">
              <thead class="text-slate-400"><tr><th class="py-1">段</th><th>字符</th><th>hash</th><th>状态</th></tr></thead>
              <tbody class="text-slate-600">
                <tr v-for="(info, key) in context.segments || {}" :key="key" class="border-t">
                  <td class="py-1">{{ SEG_LABEL[key] || key }}</td>
                  <td>{{ info.chars || 0 }}</td>
                  <td class="font-mono text-slate-400">{{ info.hash || '—' }}</td>
                  <td>
                    <span v-if="!info.present" class="text-slate-300">未注入</span>
                    <span v-else-if="info.truncated" class="text-amber-600">截断</span>
                    <span v-else class="text-emerald-600">注入</span>
                  </td>
                </tr>
              </tbody>
            </table>
            <p v-if="context.diff?.added?.length || context.diff?.removed?.length" class="text-[11px] text-slate-500">
              变化: 新增 {{ (context.diff?.added || []).join(', ') || '无' }} ·
              移除 {{ (context.diff?.removed || []).join(', ') || '无' }}
            </p>
          </section>

          <section v-else-if="tab === 'calls'" class="space-y-1">
            <p v-if="!calls.length" class="text-slate-400">该节点没有子调用</p>
            <div v-for="(c, i) in calls" :key="i" class="rounded border bg-slate-50 p-2">
              <div class="flex flex-wrap items-center gap-2">
                <span class="rounded bg-white px-1 text-[10px] text-slate-500">{{ c.span_type }}</span>
                <span class="font-mono text-[11px] text-slate-700">{{ c.name }}</span>
                <span class="text-[10px]" :class="STATUS_CLS[c.status]">{{ c.status }}</span>
                <span class="ml-auto text-[10px] text-slate-400">{{ fmtMs(c.latency_ms) }}</span>
              </div>
              <p v-if="c.tokens?.prompt || c.tokens?.completion" class="mt-0.5 text-[10px] text-slate-400">
                tokens {{ c.tokens.prompt || 0 }}/{{ c.tokens.completion || 0 }}
              </p>
            </div>
          </section>

          <section v-else-if="tab === 'raw'" class="space-y-2">
            <p class="rounded border border-amber-200 bg-amber-50 p-2 text-[11px] text-amber-800">
              原始数据可能包含案情、完整 prompt 与 state, 仅本机审计使用。
            </p>
            <div v-if="rawLoading" class="py-6 text-center text-slate-400">加载原始数据…</div>
            <template v-else-if="detail.raw">
              <p class="text-slate-400">
                span: {{ detail.raw.span_input != null || detail.raw.span_output != null ? '有原文' : '无原文' }}
              </p>
              <pre class="max-h-96 overflow-auto rounded border bg-slate-50 p-2 text-[10px]">{{ rawText() }}</pre>
            </template>
            <button v-else type="button" class="rounded border px-2 py-1 text-blue-600"
                    @click="emit('load-raw')">加载原始数据</button>
          </section>
        </template>

        <p v-else class="py-10 text-center text-sm text-slate-400">未选择节点</p>
      </div>
    </aside>
  </div>
</template>
