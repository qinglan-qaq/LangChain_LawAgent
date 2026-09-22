<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { Motion } from 'motion-v'
import { X } from 'lucide-vue-next'
import CitationList from './CitationList.vue'
import { getDialogue } from '../api'
import MarkdownView from './MarkdownView.vue'

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

// ── 对话日志(GET /api/sessions/{sid}/dialogue) ──
// 抽屉打开且带 session_id 时拉取; 换会话先置 null 防串档;
// 失败/空态(rounds+confirms 全空且无 final)统一归 null → 节内显示空态文案
const dialogue = ref(null)
const finalSection = ref(null) // 「最终答复」details 元素, 供终答落点跳转

watch(
  () => [props.open, props.detail?.session_id],
  ([open, sid]) => {
    dialogue.value = null
    if (!open || !sid) return
    getDialogue(sid)
      .then((d) => {
        const empty =
          !d ||
          (!d.rounds?.length && !d.confirms?.length && !d.final)
        dialogue.value = empty ? null : d
      })
      .catch(() => {
        dialogue.value = null
      })
  },
)

// 被选中的选项: selected_type 为 option 且文本一致才高亮(amber 强调色)
function isChosen(r, opt) {
  return r.selected_type === 'option' && opt === r.selected
}

// 终答落点: 展开「最终答复」节并平滑滚动过去
function goToFinal() {
  const el = finalSection.value
  if (!el) return
  el.open = true
  el.scrollIntoView({ behavior: 'smooth' })
}
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
        <!-- 0. 对话日志(聚合 API, 回溯主场景, 默认展开) -->
        <details open id="dialogue-log" class="border rounded-lg p-2 bg-slate-50 text-xs">
          <summary class="cursor-pointer text-slate-500 mb-1">对话日志</summary>
          <div v-if="dialogue" class="space-y-2">
            <!-- 轮次时间线 -->
            <div v-if="dialogue.rounds?.length" class="space-y-2">
              <div
                v-for="r in dialogue.rounds"
                :key="r.round"
                class="border-l border-slate-200 pl-2 space-y-1"
              >
                <div class="flex gap-1.5 flex-wrap items-baseline">
                  <span class="font-mono text-slate-500">第{{ r.round }}轮</span>
                  <span class="text-slate-800">{{ r.question }}</span>
                </div>
                <div v-if="r.options?.length" class="flex flex-wrap gap-1">
                  <span
                    v-for="opt in r.options"
                    :key="opt"
                    class="rounded px-1.5 py-0.5 border text-slate-600"
                    :class="isChosen(r, opt) ? 'bg-amber-100 border-amber-300 text-amber-800 font-medium' : 'border-slate-200'"
                  >
                    {{ opt }}
                  </span>
                </div>
                <div v-if="r.selected === null" class="text-slate-400">未作答(停在追问)</div>
                <div v-if="r.selected_type === 'free_text'" class="text-slate-600">
                  自述: {{ r.free_text }}
                </div>
                <div v-if="r.element_keys?.length" class="text-slate-400 font-mono">
                  <span v-for="k in r.element_keys" :key="k" class="mr-1">#{{ k }}</span>
                </div>
                <div v-if="r.ts" class="text-slate-300 text-[10px]">{{ r.ts }}</div>
              </div>
            </div>
            <!-- 确认类决策 -->
            <div v-if="dialogue.confirms?.length" class="space-y-1">
              <div
                v-for="(c, i) in dialogue.confirms"
                :key="i"
                class="flex gap-1 flex-wrap items-baseline"
              >
                <span class="rounded px-1.5 py-0.5 bg-slate-100 text-slate-500">{{ c.type }}</span>
                <span v-if="c.question" class="text-slate-700">{{ c.question }}</span>
                <span class="text-slate-400">→</span>
                <span class="text-slate-800">{{ c.chosen }}</span>
              </div>
            </div>
            <!-- 终答落点 -->
            <button
              v-if="dialogue.final"
              class="text-slate-500 hover:underline cursor-pointer"
              @click="goToFinal"
            >
              已产出终答 · 引用 {{ dialogue.final.citations?.length || 0 }} 条
            </button>
          </div>
          <div v-else class="text-slate-400">暂无对话日志</div>
        </details>

        <!-- 1. 最终答复(md 渲染) -->
        <details ref="finalSection" open class="border rounded-lg p-2 bg-slate-50 text-xs">
          <summary class="cursor-pointer text-slate-500 mb-1">最终答复</summary>
          <div v-if="detail?.final_answer" class="max-h-64 overflow-y-auto text-slate-700">
            <MarkdownView :text="detail.final_answer" />
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
