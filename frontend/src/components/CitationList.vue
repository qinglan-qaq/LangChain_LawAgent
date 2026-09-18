<script setup>
defineProps({ sources: { type: Array, default: () => [] } })
</script>

<template>
  <div v-if="sources.length" class="mt-2">
    <div class="text-xs text-slate-500 mb-1">引用来源</div>
    <ul>
      <li
        v-for="(s, i) in sources"
        :key="i"
        class="font-citation text-xs text-slate-700 leading-5"
      >
        [{{ i + 1 }}] {{ s.title || s.law_title || s.case_number }} {{ s.content || s.snippet || '' }}
      </li>
    </ul>
  </div>
</template>
