<script setup>
import { state } from '../store'

const STATUS_UI = {
  known: { label: '已明确', cls: 'text-green-600' },
  missing: { label: '待补齐', cls: 'text-red-500' },
  na: { label: '不适用', cls: 'text-slate-400' },
}
</script>

<template>
  <!-- L4: 仅案件问询展示(闲聊/寒暄后端标记 is_case_query=false, 保持隐藏) -->
  <div v-if="state.caseQuery && state.elements.length" id="elements" class="element-panel my-2 border rounded-lg p-2 bg-slate-50 text-xs">
    <div class="mb-1 text-slate-500">案件要素</div>
    <ul class="grid grid-cols-2 gap-1">
      <li v-for="e in state.elements" :key="e.key">
        <span class="text-slate-800">{{ e.label }}</span>
        <span class="ml-1" :class="STATUS_UI[e.status]?.cls || ''">
          ({{ STATUS_UI[e.status]?.label || e.status }}{{ e.value ? ': ' + e.value : '' }})
        </span>
      </li>
    </ul>
  </div>
</template>
