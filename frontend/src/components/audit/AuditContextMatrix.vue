<!-- AuditContextMatrix.vue
  节点 × 上下文段矩阵: chars/截断/继承标记; 点击行打开节点详情。
-->
<script setup>
import { computed } from 'vue'

const props = defineProps({
  context: { type: Object, default: () => ({}) },
  selectedStageId: { type: Number, default: null },
})
const emit = defineEmits(['select'])

const SEG_LABEL = {
  task: '任务',
  current_query: '提问',
  critical_facts: '要素',
  history: '历史',
  evidence: '证据',
  constraints: '约束',
  memory: '记忆',
}

const segments = computed(() => props.context?.segments || [])
const nodes = computed(() => props.context?.nodes || [])
const summary = computed(() => props.context?.summary || {})

function cell(node, seg) {
  return node?.segments?.[seg] || {}
}
</script>

<template>
  <div class="audit-context-matrix space-y-2">
    <div class="flex flex-wrap gap-3 rounded-lg border bg-white p-2 text-[11px] text-slate-500">
      <span>总注入 {{ summary.total_chars ?? 0 }} 字符</span>
      <span>历史 {{ summary.history_turns == null ? '—' : `${summary.history_turns} 轮` }}</span>
      <span>记忆命中 {{ summary.memory_hits ?? 0 }}</span>
      <span v-if="summary.truncated_segments?.length" class="text-amber-600">
        截断段: {{ summary.truncated_segments.join(' / ') }}
      </span>
      <span class="ml-auto text-slate-400">↳ 继承上游 · ! 截断 · 灰=未注入</span>
    </div>

    <div class="overflow-x-auto rounded-lg border bg-white">
      <table class="w-full text-left text-[11px]">
        <thead class="bg-slate-50 text-slate-400">
          <tr>
            <th class="sticky left-0 bg-slate-50 px-2 py-1">节点</th>
            <th v-for="s in segments" :key="s" class="px-2 py-1">{{ SEG_LABEL[s] || s }}</th>
          </tr>
        </thead>
        <tbody class="text-slate-600">
          <tr v-for="n in nodes" :key="n.stage_id"
              class="cursor-pointer border-t hover:bg-blue-50/40"
              :class="n.stage_id === selectedStageId ? 'bg-blue-50/60' : ''"
              @click="emit('select', n.stage_id)">
            <td class="sticky left-0 whitespace-nowrap bg-inherit px-2 py-1 font-mono">
              {{ n.node_name }}
              <span v-if="n.inherited_from_stage_id" class="ml-1 text-[10px] text-slate-400"
                    :title="`继承自 #${n.inherited_from_stage_id}`">↳</span>
            </td>
            <td v-for="s in segments" :key="s" class="px-2 py-1">
              <template v-if="cell(n, s).present">
                <span :class="cell(n, s).injected ? 'text-slate-700' : 'text-slate-300'">
                  {{ cell(n, s).chars || 0 }}
                </span>
                <span v-if="cell(n, s).truncated" class="ml-0.5 text-amber-600" title="该段被截断">!</span>
              </template>
              <span v-else class="text-slate-200">·</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <p v-if="!nodes.length" class="text-xs text-slate-400">暂无上下文采集</p>
  </div>
</template>
