<!-- JsonTree.vue
  JSON 分层树(D6 增强): 阶段输入/输出/入参/结果/token 的层级折叠展示,
  CSS 自绘主题色(slate 底 + 类型着色), 不引图表库。
  分支节点(对象/数组)可折叠, 折叠态显示 {n}/[n] 概要; 初始展开 defaultOpen 层。
-->
<script setup>
import { computed, ref, watch } from 'vue'

const props = defineProps({
  node: { type: null, required: true },
  name: { type: String, default: '' },
  depth: { type: Number, default: 0 },
  defaultOpen: { type: Number, default: 1 },
  treeId: { type: String, default: '' },   // 仅根层实例挂此 id(递归子层不挂)
})

const isBranch = computed(
  () => props.node !== null && typeof props.node === 'object'
)
const isArray = computed(() => Array.isArray(props.node))
const open = ref(props.depth < props.defaultOpen)

// data 引用变化时(切阶段/切 run)按层级重置展开态
watch(
  () => props.node,
  () => { open.value = props.depth < props.defaultOpen }
)

const entries = computed(() =>
  isArray.value
    ? props.node.map((v, i) => [String(i), v])
    : Object.entries(props.node ?? {})
)
const summary = computed(() =>
  isArray.value ? `[${props.node.length}]` : `{${entries.value.length}}`
)

function typeCls(v) {
  if (v === null || v === undefined) return 'text-slate-400'
  if (typeof v === 'string') return 'text-emerald-600'
  if (typeof v === 'number') return 'text-blue-600'
  if (typeof v === 'boolean') return 'text-amber-600'
  return 'text-slate-500'
}
// 长字符串截断显示, 全文挂 title
function fmtLeaf(v) {
  if (v === null || v === undefined) return 'null'
  if (typeof v === 'string') return v.length > 300 ? v.slice(0, 300) + '…' : v
  return JSON.stringify(v)
}
</script>

<template>
  <!-- 分支: 折叠箭头 + key + {n}/[n] 概要或子层(递归实例不重复挂 id) -->
  <div v-if="isBranch" :id="treeId || null" class="json-tree leading-5">
    <div class="flex items-start gap-0.5">
      <button
        class="w-3.5 shrink-0 text-left text-slate-400 hover:text-slate-700"
        :aria-label="open ? '折叠' : '展开'"
        @click="open = !open"
      >
        {{ open ? '▾' : '▸' }}
      </button>
      <span v-if="name" class="text-slate-600 font-medium">{{ name }}:</span>
      <button
        v-if="!open"
        class="text-slate-400 hover:text-slate-600"
        @click="open = true"
      >
        {{ summary }}
      </button>
      <span v-else class="text-slate-400">{{ isArray ? '[' : '{' }}</span>
    </div>
    <div v-if="open" class="pl-4 border-l border-slate-200 ml-1">
      <div v-if="!entries.length" class="text-slate-400 pl-1">空</div>
      <JsonTree
        v-for="[k, v] in entries"
        :key="k"
        :node="v"
        :name="k"
        :depth="depth + 1"
        :default-open="defaultOpen"
      />
      <div class="text-slate-400">{{ isArray ? ']' : '}' }}</div>
    </div>
  </div>
  <!-- 叶子: key: 值(类型着色) -->
  <div v-else :id="treeId ? `${treeId}-leaf` : null" class="json-tree-leaf leading-5">
    <span v-if="name" class="text-slate-600 font-medium">{{ name }}: </span>
    <span :class="typeCls(node)" :title="String(node)">{{
      typeof node === 'string' ? `“${fmtLeaf(node)}”` : fmtLeaf(node)
    }}</span>
  </div>
</template>
