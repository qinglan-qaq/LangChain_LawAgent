<script setup>
import { computed, ref, watch } from 'vue'
import { Motion } from 'motion-v'

// 跟消息走的思考过程(需求1/2): 挂在 assistant 气泡下方, 数据来自消息上的
// thinking 快照(ChatPage 每收一个 SSE 事件镜像一次全局回合状态)
const props = defineProps({
  thinking: { type: Object, default: null },
  active: { type: Boolean, default: false }, // 流式期(最后一条未完成)自动展开
})

const open = ref(false)
watch(
  () => props.active,
  (a) => {
    open.value = a
  },
  { immediate: true },
)

const hasContent = computed(() => {
  const t = props.thinking
  return !!(
    t &&
    (t.status || t.planText || t.reasoning || t.promptsLog ||
      (t.tools && t.tools.length) || (t.steps && t.steps.length))
  )
})

const badge = computed(() => {
  const t = props.thinking
  if (!t) return ''
  const parts = []
  if (t.tools && t.tools.length) parts.push(`工具 ${t.tools.length}`)
  if (t.steps && t.steps.length) parts.push(`步骤 ${t.steps.length}`)
  return parts.join(' · ')
})
</script>

<template>
  <details
    v-if="hasContent"
    :open="open"
    class="message-thinking ml-1 mt-1 border-l-2 border-slate-200 pl-2 py-0.5 text-[11px] bg-transparent"
  >
    <summary class="cursor-pointer select-none text-slate-400">
      思考过程
      <span v-if="badge" class="text-slate-400">{{ badge }}</span>
      <span v-if="active" class="animate-pulse">▍</span>
    </summary>

    <div class="mt-1 space-y-2">
      <!-- 工作状态: 后端 status 事件文本 -->
      <div v-if="thinking.status" class="flex items-center gap-1.5 text-slate-500">
        <span class="inline-block h-2.5 w-2.5 shrink-0 rounded-full border-2 border-slate-300 border-t-slate-600 animate-spin" />
        <span>{{ thinking.status }}</span>
      </div>

      <!-- 执行进度: progress 解析步骤, 当前步 mini spinner -->
      <div v-if="thinking.steps && thinking.steps.length">
        <div class="text-slate-400">执行进度</div>
        <ol>
          <Motion
            v-for="(s, i) in thinking.steps"
            :key="i"
            as="li"
            class="flex items-center gap-1.5 py-0.5"
            :initial="{ opacity: 0, y: 3 }"
            :animate="{ opacity: 1, y: 0 }"
          >
            <span v-if="s.done" class="text-green-600">✓</span>
            <span v-else-if="s.current" class="inline-block h-2 w-2 shrink-0 rounded-full border-2 border-slate-300 border-t-slate-600 animate-spin" />
            <span v-else class="text-slate-400">○</span>
            <span :class="s.current ? 'text-slate-600 font-medium' : 'text-slate-500'">{{ s.text }}</span>
          </Motion>
        </ol>
      </div>

      <!-- 计划内容: planner/replanner 流 -->
      <div v-if="thinking.planText">
        <div class="text-slate-400">计划</div>
        <pre class="whitespace-pre-wrap mt-0.5 font-mono text-slate-500">{{ thinking.planText }}</pre>
      </div>

      <!-- 工具调用(需求2): 🔧 正在调用 → ✓ 完成 · 结果摘要 -->
      <div v-if="thinking.tools && thinking.tools.length">
        <div class="text-slate-400">工具调用</div>
        <ol>
          <Motion
            v-for="(item, i) in thinking.tools"
            :key="i"
            as="li"
            class="flex items-center gap-1.5 py-0.5"
            :initial="{ opacity: 0, y: 3 }"
            :animate="{ opacity: 1, y: 0 }"
          >
            <template v-if="item.result">
              <span class="text-green-600">✓</span>
              <span class="font-mono text-slate-600">{{ item.name }}</span>
              <span class="truncate text-slate-400">· {{ item.result }}</span>
            </template>
            <template v-else>
              <span>🔧</span>
              <span class="text-slate-500">正在调用 <span class="font-mono">{{ item.name }}</span></span>
              <span class="inline-block h-2 w-2 shrink-0 rounded-full border-2 border-slate-300 border-t-slate-600 animate-spin" />
            </template>
          </Motion>
        </ol>
      </div>

      <!-- CoT: reasoning 流累积, 出错红框 -->
      <div v-if="thinking.reasoning">
        <div class="text-slate-400">思考过程(CoT)</div>
        <pre
          class="whitespace-pre-wrap mt-0.5 text-slate-500"
          :class="{ 'border border-red-300 rounded p-1': thinking.reasoningError }"
        >{{ thinking.reasoning }}</pre>
      </div>

      <!-- 提示词记录 -->
      <div v-if="thinking.promptsLog">
        <div class="text-slate-400">提示词记录</div>
        <pre class="whitespace-pre-wrap mt-0.5 font-mono text-slate-400">{{ thinking.promptsLog }}</pre>
      </div>
    </div>
  </details>
</template>
