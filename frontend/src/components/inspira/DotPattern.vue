<!--
  DotPattern — Inspira UI (https://inspira-ui.com) 风格组件
  官方站点页面不可达, 按 Magic UI DotPattern 的公开行为手写; 语义一致:
  SVG pattern 圆点阵, 绝对定位铺满父容器(父容器需 relative)。
-->
<script setup>
import { useId } from 'vue'
import { cn } from '../../lib/utils'

const props = defineProps({
  width: { type: Number, default: 16 },
  height: { type: Number, default: 16 },
  cx: { type: Number, default: 1 },
  cy: { type: Number, default: 1 },
  cr: { type: Number, default: 1 },
  class: { type: String, default: '' },
})
const id = useId()
</script>

<template>
  <svg
    aria-hidden="true"
    :class="cn('pointer-events-none absolute inset-0 h-full w-full fill-slate-400/40', props.class)"
  >
    <defs>
      <pattern :id="id" :width="width" :height="height" patternUnits="userSpaceOnUse">
        <circle :cx="cx" :cy="cy" :r="cr" />
      </pattern>
    </defs>
    <rect width="100%" height="100%" :fill="`url(#${id})`" />
  </svg>
</template>
