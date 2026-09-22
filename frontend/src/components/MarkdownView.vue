<script setup>
import { computed } from 'vue'
import MarkdownIt from 'markdown-it'

// md 渲染组件: 供 LLM 输出/回溯内容使用
// html:false 转义原始 HTML 标签(输出源为自有 LLM, 仍按不信任文本处理);
// breaks:true 让单个换行成 <br>(LLM 输出习惯); linkify 自动识别 URL
const props = defineProps({
  text: { type: String, default: '' },
})

const md = new MarkdownIt({ html: false, linkify: true, breaks: true })
const html = computed(() => md.render(props.text || ''))
</script>

<template>
  <div class="md-body" v-html="html" />
</template>
