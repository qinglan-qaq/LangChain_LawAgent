<script setup>
import { state } from '../store'
</script>

<template>
  <!-- 计划内容实时流: planner/replanner 思考(CoT)完成后, 计划 JSON 内容逐字流出
       (后端 _stream_plan content 增量, source=planner_plan/replanner_plan) -->
  <details
    v-if="state.planText"
    open
    class="border rounded-lg p-2 my-1 text-xs bg-slate-50 border-slate-200"
  >
    <summary class="cursor-pointer select-none">计划内容</summary>
    <pre class="whitespace-pre-wrap mt-1 font-mono text-slate-600">{{ state.planText }}</pre>
  </details>
</template>
