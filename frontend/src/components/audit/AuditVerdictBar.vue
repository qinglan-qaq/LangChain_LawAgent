<!-- AuditVerdictBar.vue
  四张固定结论卡: 流程/证据/上下文/性能; 点击跳到对应审计区段。
-->
<script setup>
import { computed } from 'vue'
import { LEVEL_UI, levelOf } from './auditUi'

const props = defineProps({
  verdicts: { type: Object, default: () => ({}) },
})
const emit = defineEmits(['jump'])

const ORDER = [
  ['flow', '流程', '流程是否完整走到终答/人工中断'],
  ['evidence', '证据链', '引用能否回到文档与工具调用'],
  ['context', '上下文', '各节点注入的上下文段是否正确'],
  ['performance', '性能', '慢点/错误/tokens/人工等待'],
]

const cards = computed(() =>
  ORDER.map(([key, label, hint]) => {
    const v = props.verdicts?.[key] || { key, level: 'unknown' }
    return { key, label, hint, ...v, ui: levelOf(v.level) }
  })
)

function jump(card) {
  emit('jump', card.jump?.section || 'timeline')
}
</script>

<template>
  <section class="audit-verdict-bar grid grid-cols-2 gap-2 lg:grid-cols-4">
    <button v-for="c in cards" :key="c.key"
            type="button"
            class="rounded-lg border p-2 text-left transition-shadow hover:shadow"
            :class="c.ui.cls"
            :title="c.hint"
            @click="jump(c)">
      <div class="flex items-center gap-1.5">
        <span class="text-xs font-semibold">{{ c.label }}</span>
        <span class="rounded bg-white/70 px-1 text-[10px]">{{ c.ui.label }}</span>
        <span class="ml-auto text-[10px] opacity-60">{{ c.jump?.section || 'timeline' }} →</span>
      </div>
      <p class="mt-1 line-clamp-2 text-xs">{{ c.summary || '—' }}</p>
      <p v-if="c.reasons?.length" class="mt-0.5 line-clamp-2 text-[10px] opacity-75">
        {{ c.reasons.join(' · ') }}
      </p>
    </button>
  </section>
</template>
