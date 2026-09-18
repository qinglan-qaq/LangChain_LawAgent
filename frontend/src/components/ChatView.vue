<script setup>
import { computed, ref, watch } from 'vue'
import { state, useTypewriter } from '../store'
import FadeIn from './inspira/FadeIn.vue'

// 流式目标: 最后一条 assistant 消息(打字机只对它生效)
const lastAssistant = computed(
  () => [...state.value.messages].reverse().find((m) => m.role === 'assistant') || null,
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
</script>

<template>
  <div class="space-y-3">
    <FadeIn v-for="(m, i) in state.messages" :key="i" :delay="0">
      <div :class="m.role === 'user' ? 'flex justify-end' : 'flex justify-start'">
        <div
          class="max-w-[80%] rounded-2xl px-4 py-2 text-sm leading-6"
          :class="m.role === 'user' ? 'bg-slate-900 text-white' : 'bg-white border'"
        >
          <!-- 用户消息: 超长默认折叠, 点击展开(用户决策「提问可折叠」) -->
          <template v-if="m.role === 'user'">
            <span class="whitespace-pre-wrap">{{
              m.collapsed && !m.expanded ? m.text.slice(0, 120) + '…' : m.text
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
    </FadeIn>
  </div>
</template>
