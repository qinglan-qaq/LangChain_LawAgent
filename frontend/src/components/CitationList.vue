<script setup>
defineProps({ sources: { type: Array, default: () => [] } })
</script>

<template>
  <div v-if="sources.length" class="mt-2">
    <div class="text-xs text-slate-500 mb-1">引用来源</div>
    <ul>
      <!-- L9: 列表源 filter(Boolean) 防混入 null/undefined; 项级 ?. 链 +
           兜底默认值, 不再因缺字段渲染出 "undefined" 或整行空白 -->
      <li
        v-for="(s, i) in sources.filter(Boolean)"
        :key="i"
        class="font-citation text-xs text-slate-700 leading-5"
      >
        [{{ i + 1 }}] {{ s?.title || s?.law_title || s?.case_number || '未命名' }}
        {{ s?.content || s?.snippet || '' }}
      </li>
    </ul>
  </div>
</template>
