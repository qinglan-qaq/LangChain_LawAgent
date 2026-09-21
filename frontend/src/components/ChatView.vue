<script setup>
import { computed, ref, watch } from 'vue'
import { Motion } from 'motion-v'
import { state, useTypewriter, COLLAPSE_LEN } from '../store'

// 流式目标: 最后一条普通 assistant 消息(打字机只对它生效);
// HITL 追问/回答消息(kind=hitl_*)不参与打字机, 静态整段渲染
const lastAssistant = computed(
  () => [...state.value.messages].reverse().find((m) => m.role === 'assistant' && !m.kind) || null,
)
const full = computed(() => lastAssistant.value?.text ?? '')
const shown = ref('')
const tw = useTypewriter(full, shown)

// token 流入时启动追平; 换新一条 assistant 消息时清空已显示文本
watch(full, () => {
  if (full.value) tw.start()
})
watch(lastAssistant, () => {
  shown.value = ''
})

// 气泡样式: HITL 追问用淡琥珀边框(区分普通回答), 其余按角色
function bubbleClass(m) {
  if (m.kind === 'hitl_question') return 'bg-white border border-amber-300'
  return m.role === 'user' ? 'bg-slate-900 text-white' : 'bg-white border'
}

// HITL 追问标签: 澄清类(clarify/mid_clarify)为「追问」, 其余为「人工确认」
function hitlTag(m) {
  return /clarify/.test(m.hitl_type || '') ? '追问' : '人工确认'
}
</script>

<template>
  <div class="space-y-3">
    <!-- 官方 Inspira 无 FadeIn 组件, 消息入场按官方动画底座 motion-v 直写 -->
    <Motion
      v-for="(m, i) in state.messages"
      :key="i"
      as="div"
      :initial="{ opacity: 0, y: 4 }"
      :animate="{ opacity: 1, y: 0 }"
    >
      <div :class="m.role === 'user' ? 'flex justify-end' : 'flex justify-start'">
        <div
          class="max-w-[80%] rounded-2xl px-4 py-2 text-sm leading-6"
          :class="bubbleClass(m)"
        >
          <!-- HITL 追问(需求3): 类型标签 + 问题文本 + (选择题时)选项列表 -->
          <template v-if="m.kind === 'hitl_question'">
            <span class="mr-1 rounded bg-amber-100 px-1.5 py-0.5 text-[10px] text-amber-700">
              {{ hitlTag(m) }}
            </span>
            <span class="whitespace-pre-wrap">{{ m.text }}</span>
            <ul v-if="m.options && m.options.length" class="mt-1 space-y-0.5 text-slate-500">
              <li v-for="(o, oi) in m.options" :key="oi">
                · {{ typeof o === 'string' ? o : o.label }}
              </li>
            </ul>
          </template>
          <!-- HITL 回答(需求3): 用户气泡 + 小字「回答：{问题摘要}」头 -->
          <template v-else-if="m.kind === 'hitl_answer'">
            <div v-if="m.question" class="text-[10px] opacity-70 mb-0.5">
              回答：{{ m.question.slice(0, 30) }}…
            </div>
            <span class="whitespace-pre-wrap">{{ m.text }}</span>
          </template>
          <!-- 用户消息: 超长默认折叠, 点击展开(用户决策「提问可折叠」) -->
          <template v-else-if="m.role === 'user'">
            <span class="whitespace-pre-wrap">{{
              m.collapsed && !m.expanded ? m.text.slice(0, COLLAPSE_LEN) + '…' : m.text
            }}</span>
            <button
              v-if="m.collapsed"
              class="block mt-1 text-xs underline opacity-70"
              @click="m.expanded = !m.expanded"
            >
              {{ m.expanded ? '收起' : '展开全文' }}
            </button>
          </template>
          <!-- assistant 消息: 打字机 + 流式光标 -->
          <template v-else>
            <span class="whitespace-pre-wrap">{{
              m === lastAssistant && !m.done ? shown : m.text
            }}</span>
            <span v-if="m === lastAssistant && !m.done" class="animate-pulse">▍</span>
          </template>
        </div>
      </div>
    </Motion>
  </div>
</template>
