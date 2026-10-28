<!-- ContextMetaPanel.vue
  上下文信封元信息: 各段字符数 / hash / 是否截断(§九 段级预算的可观测面)。
  完整 meta 仍可折叠查看, 默认只显示一级段概览。
-->
<script setup>
import { computed } from 'vue'
import JsonTree from './JsonTree.vue'

const props = defineProps({
  meta: { type: Object, default: () => ({}) },
})

const segments = computed(() =>
  Object.entries(props.meta || {})
    .filter(([, v]) => v && typeof v === 'object' && typeof v.chars === 'number')
    .map(([key, v]) => ({ key, chars: v.chars, hash: v.hash }))
)
const totalChars = computed(() => segments.value.reduce((a, b) => a + b.chars, 0))
const truncated = computed(() => Boolean(props.meta?.truncated))
</script>

<template>
  <div id="context-meta-panel" class="context-meta-panel space-y-2">
    <div class="flex flex-wrap items-center gap-2 text-xs">
      <span class="rounded bg-slate-100 px-1.5 py-0.5 text-slate-600">
        上下文 {{ totalChars }} 字符
      </span>
      <span v-if="truncated" class="rounded bg-amber-50 px-1.5 py-0.5 text-amber-700">
        有段触发预算截断
      </span>
      <span v-else class="rounded bg-emerald-50 px-1.5 py-0.5 text-emerald-700">
        完整注入
      </span>
    </div>

    <div v-if="segments.length" id="context-meta-segments" class="space-y-1">
      <div v-for="s in segments" :key="s.key"
           :id="`context-meta-seg-${s.key}`"
           class="flex items-center gap-2 text-xs">
        <span class="w-28 shrink-0 truncate font-mono text-slate-500">{{ s.key }}</span>
        <div class="h-1.5 flex-1 overflow-hidden rounded-full bg-slate-100">
          <div class="h-full rounded-full bg-sky-400"
               :style="{ width: `${Math.min(100, (s.chars / Math.max(totalChars, 1)) * 100)}%` }" />
        </div>
        <span class="w-14 text-right text-slate-500">{{ s.chars }} 字</span>
        <span class="w-16 text-right font-mono text-[10px] text-slate-400">{{ s.hash }}</span>
      </div>
    </div>
    <p v-else class="text-xs text-slate-400">无上下文 meta(该节点未走信封)</p>

    <details class="text-xs">
      <summary class="cursor-pointer text-slate-400 hover:text-slate-600">原始 meta JSON</summary>
      <div class="mt-1 max-h-48 overflow-auto rounded border bg-slate-50 p-2 font-mono text-[11px]">
        <JsonTree :node="meta" name="context_meta" :default-open="1"
                  tree-id="context-meta-tree" />
      </div>
    </details>
  </div>
</template>
