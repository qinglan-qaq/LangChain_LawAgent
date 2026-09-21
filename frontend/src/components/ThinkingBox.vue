<script setup>
import { computed, ref, watch } from 'vue'
import { Motion } from 'motion-v'
import { state } from '../store'

// 整合面板(需求2/5): 单个可折叠 details 合并 运行状态/执行进度/计划/工具调用/CoT/提示词记录,
// 替代原先 StatusBar+ThinkingPanel+PlanPanel+ToolTimeline 四个平铺组件
const open = ref(false)

// 忙时自动展开(看实时进度), 空闲自动收起(给对话区让位); 用户仍可手动开关
watch(
  () => state.value.busy || state.value.reasoningActive,
  (active) => {
    open.value = active
  },
)

// 任一区块有内容才渲染整个面板(空闲且无历史时不显示空壳)
const hasContent = computed(
  () =>
    state.value.status ||
    state.value.steps.length ||
    state.value.planText ||
    state.value.tools.length ||
    state.value.reasoning ||
    state.value.promptsLog,
)

// 工具使用记录 JSON 是否有内容(内嵌二级 details)
const hasToolUsage = computed(() => Object.keys(state.value.toolUsage).length > 0)
</script>

<template>
  <details
    v-if="hasContent"
    :open="open"
    class="border rounded-lg p-2 my-1 text-xs bg-slate-50 border-slate-200"
  >
    <summary class="cursor-pointer select-none">
      {{ state.reasoningError ? '思考过程(中断, 已保留片段)' : '思考过程' }}
      <span v-if="state.reasoningActive" class="animate-pulse">▍</span>
    </summary>

    <!-- 运行状态: spinner + 状态文本; 思考中整行呼吸提示(status 变更即有动效反馈) -->
    <div
      v-if="state.status"
      class="flex items-center gap-2 mt-1 text-slate-500"
      :class="{ 'animate-pulse': state.reasoningActive || state.busy }"
    >
      <span
        class="inline-block h-3 w-3 shrink-0 rounded-full border-2 border-slate-300 border-t-slate-600 animate-spin"
      />
      <span>{{ state.status }}</span>
    </div>

    <!-- 执行进度: progress 事件解析出的步骤列表, 当前步带 mini spinner 防「像卡住」误判 -->
    <template v-if="state.steps.length">
      <div class="mt-2 text-slate-500">执行进度</div>
      <ol>
        <Motion
          v-for="(s, i) in state.steps"
          :key="i"
          as="li"
          class="flex items-center gap-2 py-0.5"
          :initial="{ opacity: 0, y: 4 }"
          :animate="{ opacity: 1, y: 0 }"
        >
          <span v-if="s.done" class="text-green-600">✓</span>
          <span
            v-else-if="s.current"
            class="inline-block h-2 w-2 shrink-0 rounded-full border-2 border-slate-300 border-t-slate-600 animate-spin"
          />
          <span v-else class="text-slate-400">○</span>
          <span :class="s.current ? 'text-slate-700 font-medium' : 'text-slate-500'">{{ s.text }}</span>
        </Motion>
      </ol>
    </template>

    <!-- 计划: planner/replanner 计划内容实时流(source=*_plan) -->
    <template v-if="state.planText">
      <div class="mt-2 text-slate-500">计划</div>
      <pre class="whitespace-pre-wrap mt-1 font-mono text-slate-600">{{ state.planText }}</pre>
    </template>

    <!-- 工具调用: 无结果时 mini spinner, 有结果给摘要(沿 ToolTimeline 样式) -->
    <template v-if="state.tools.length">
      <div class="mt-2 text-slate-500">工具调用</div>
      <ol>
        <Motion
          v-for="(item, i) in state.tools"
          :key="i"
          as="li"
          class="flex items-center gap-2 py-0.5"
          :initial="{ opacity: 0, y: 6 }"
          :animate="{ opacity: 1, y: 0 }"
        >
          <span class="text-slate-400">[{{ i + 1 }}]</span>
          <span class="font-mono">{{ item.name }}</span>
          <span v-if="item.result" class="truncate text-slate-500">{{ item.result }}</span>
          <span
            v-else
            class="inline-block h-2 w-2 shrink-0 rounded-full border-2 border-slate-300 border-t-slate-600 animate-spin"
          />
        </Motion>
      </ol>
      <!-- 工具使用 JSON 记录: {toolName: [结果摘要, ...]}(后端 tool_usage 事件) -->
      <details v-if="hasToolUsage" class="mt-1">
        <summary class="cursor-pointer select-none text-slate-500">工具使用记录(JSON)</summary>
        <pre class="whitespace-pre-wrap mt-1 font-mono text-slate-500">{{
          JSON.stringify(state.toolUsage, null, 2)
        }}</pre>
      </details>
    </template>

    <!-- 思考过程(CoT): reasoning 流累积文本, 出错时红框 -->
    <template v-if="state.reasoning">
      <div class="mt-2 text-slate-500">思考过程(CoT)</div>
      <pre
        class="whitespace-pre-wrap mt-1 text-slate-600"
        :class="{ 'border border-red-300 rounded p-1': state.reasoningError }"
      >{{ state.reasoning }}</pre>
    </template>

    <!-- 提示词记录: prompts_record/final_prompts 事件逐行追加(单行截断) -->
    <template v-if="state.promptsLog">
      <div class="mt-2 text-slate-500">提示词记录</div>
      <pre class="whitespace-pre-wrap mt-1 font-mono text-slate-500">{{ state.promptsLog }}</pre>
    </template>
  </details>
</template>
