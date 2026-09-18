<script setup>
import { state } from '../store'
import Timeline from './inspira/Timeline.vue'
</script>

<template>
  <div
    v-if="state.tools.length"
    class="my-2 text-xs text-slate-600 border rounded-lg p-2 bg-slate-50"
  >
    <div class="mb-1 text-slate-500">工具调用</div>
    <Timeline :items="state.tools">
      <template #default="{ item, index }">
        <span class="text-slate-400">[{{ index + 1 }}]</span>
        <span class="font-mono">{{ item.name }}</span>
        <span v-if="item.result" class="truncate text-slate-500">{{ item.result }}</span>
      </template>
    </Timeline>
  </div>
</template>
