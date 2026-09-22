<script setup>
import { computed, onMounted, onUnmounted } from 'vue'
import { Motion } from 'motion-v'
import { X } from 'lucide-vue-next'
import CitationList from './CitationList.vue'

// 任务4: 会话详情右侧抽屉(HistorySidebar 原内联详情块的替代载体)
const props = defineProps({
  detail: { type: Object, default: null },
  open: { type: Boolean, default: false },
})
const emit = defineEmits(['close'])

// 引用来源合并逻辑(自 HistorySidebar 迁来): law_results + rag_documents,
// filter(Boolean) 防后端降级/脏数据混入 null 项
function citationsOf(d) {
  if (!d) return []
  return [...(d.law_results || []), ...(d.rag_documents || [])].filter(Boolean)
}

// 案件要素状态色块(参照 ElementPanel, 抽屉内静态小标签形式)
const STATUS_UI = {
  known: { label: '已明确', cls: 'bg-green-100 text-green-700' },
  missing: { label: '待补齐', cls: 'bg-amber-100 text-amber-700' },
  na: { label: '不适用', cls: 'bg-slate-100 text-slate-500' },
}
const elements = computed(() => props.detail?.elements || [])

// 工具调用: detail.tool_usage 为 {tool_name: [结果摘要, ...]}(用户决策 v4),
// detail.tool_calls 为工具名列表(后端 QueryResponse 序列化形状, 兼容旧对象形状)
const hasToolUsage = computed(() => Object.keys(props.detail?.tool_usage || {}).length > 0)
const toolUsageEntries = computed(() => Object.entries(props.detail?.tool_usage || {}))
const toolCalls = computed(() => props.detail?.tool_calls || [])
const hasTools = computed(() => hasToolUsage.value || toolCalls.value.length > 0)
// 列表项可能是工具名字符串, 也可能是 {tool_name}/{name} 对象(旧 checkpoint)
function toolNameOf(t) {
  return typeof t === 'string' ? t : String(t?.tool_name || t?.name || '')
}

// Esc 关闭
function onKey(e) {
  if (e.key === 'Escape') emit('close')
}
onMounted(() => window.addEventListener('keydown', onKey))
onUnmounted(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <Teleport to="body">
    <!-- 遮罩: 点击即关 -->
    <div v-if="open" class="fixed inset-0 bg-black/30 z-40" @click="emit('close')" />
    <Motion
      v-if="open"
      as="div"
      :initial="{ x: 40, opacity: 0 }"
      :animate="{ x: 0, opacity: 1 }"
      class="fixed right-0 top-0 h-full w-[420px] max-w-[90vw] bg-white border-l border-slate-200 shadow-xl z-40 flex flex-col text-xs"
    >
      <div class="px-4 py-3 border-b flex items-center justify-between">
        <div class="font-mono text-slate-700">{{ detail?.session_id || '' }}</div>
        <button class="hover:bg-slate-100 rounded p-1" @click="emit('close')">
          <X class="w-4 h-4" />
        </button>
      </div>
      <div class="flex-1 overflow-y-auto p-4 space-y-3">
        <!-- 1. 最终答复 -->
        <details open class="border rounded-lg p-2 bg-slate-50 text-xs">
          <summary class="cursor-pointer text-slate-500 mb-1">最终答复</summary>
          <div
            v-if="detail?.final_answer"
            class="max-h-64 overflow-y-auto text-slate-700 leading-6 whitespace-pre-wrap"
          >
            {{ detail.final_answer }}
          </div>
          <div v-else class="text-slate-400">未产出最终回答(可能停在人工确认)</div>
        </details>

        <!-- 2. 澄清记录(沿用原内联三行结构) -->
        <details
          v-if="detail?.clarify_history?.length"
          class="border rounded-lg p-2 bg-slate-50 text-xs"
        >
          <summary class="cursor-pointer text-slate-500 mb-1">澄清记录</summary>
          <ul class="space-y-1">
            <li
              v-for="(c, i) in detail.clarify_history"
              :key="i"
              class="text-slate-600"
            >
              <div>第{{ c.round }}轮 · {{ c.question }}</div>
              <div v-if="c.options && c.options.length" class="text-slate-500">
                {{ c.options.map((o) => (typeof o === 'string' ? o : o.label)).join(' / ') }}
              </div>
              <div>用户: {{ c.answer }}</div>
            </li>
          </ul>
        </details>

        <!-- 3. 案件要素(静态色块标签) -->
        <details v-if="elements.length" class="border rounded-lg p-2 bg-slate-50 text-xs">
          <summary class="cursor-pointer text-slate-500 mb-1">案件要素</summary>
          <ul class="grid grid-cols-2 gap-1">
            <li v-for="e in elements" :key="e.key" class="flex items-center gap-1 flex-wrap">
              <span class="text-slate-800">{{ e.label }}</span>
              <span
                class="rounded px-1.5 py-0.5"
                :class="STATUS_UI[e.status]?.cls || 'bg-slate-100 text-slate-500'"
              >
                {{ STATUS_UI[e.status]?.label || e.status }}{{ e.value ? ': ' + e.value : '' }}
              </span>
            </li>
          </ul>
        </details>

        <!-- 4. 引用来源 -->
        <details class="border rounded-lg p-2 bg-slate-50 text-xs">
          <summary class="cursor-pointer text-slate-500 mb-1">引用来源</summary>
          <CitationList :sources="citationsOf(detail)" />
        </details>

        <!-- 5. 工具调用(新增轻量展示) -->
        <details v-if="hasTools" class="border rounded-lg p-2 bg-slate-50 text-xs">
          <summary class="cursor-pointer text-slate-500 mb-1">工具调用</summary>
          <ul v-if="hasToolUsage" class="space-y-1">
            <li v-for="([name, results], i) in toolUsageEntries" :key="i" class="text-slate-600">
              <span class="font-mono">{{ name }}</span>
              <span class="text-slate-500"> × {{ results.length }}</span>
              <ul v-if="results.length" class="ml-3 text-slate-500">
                <li v-for="(r, ri) in results" :key="ri">· {{ r }}</li>
              </ul>
            </li>
          </ul>
          <ul v-else-if="toolCalls.length" class="space-y-0.5">
            <li v-for="(t, i) in toolCalls" :key="i" class="text-slate-600">
              [{{ i + 1 }}] <span class="font-mono">{{ toolNameOf(t) }}</span>
            </li>
          </ul>
        </details>
      </div>
    </Motion>
  </Teleport>
</template>
