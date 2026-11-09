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

// LLM 常输出「#标题」「##标题|表头|」这类无空格/无换行的伪 Markdown,
// CommonMark 会把整行当正文或把表格吞进标题。渲染前做最小归一化。
// CommonMark 的强调定界符对中文不友好: "：**重点**你" 会被当纯文本。
// 顺序配对 ** 标记, 只在闭标记后接中文时补空格; 不用正则跨配对匹配,
// 否则相邻的两个粗体会被误当成一对。
function padCjkBold(src) {
  let out = ''
  let i = 0
  let open = false
  let start = 0
  for (;;) {
    const j = src.indexOf('**', i)
    if (j < 0) break
    if (!open) {
      out += src.slice(i, j) + '**'
      open = true
      start = j + 2
      i = start
      continue
    }
    out += src.slice(start, j) + '**'
    i = j + 2
    if (/[\u4e00-\u9fff]/.test(src[i] || '')) out += ' '
    open = false
  }
  return out + src.slice(i)
}

function normalizeMarkdown(src) {
  const prepared = (src || '')
    // 标题粘在上一段/表格行后: "…|## 七、" → 换行
    .replace(/([^\n#])(#{1,6})(?=\s|#)/g, '$1\n\n$2')
    .replace(/([^\n#])(#{1,6})(?=[^\s#])/g, '$1\n\n$2')
    // 标题标记后缺空格: "##一、" → "## 一、"
    .replace(/^(\s{0,3}#{1,6})(?=[^\s#])/gm, '$1 ')
    // 表格粘在标题/正文后, 且下一行是分隔行: "## 标题|a|b|\n|---|" → 断行
    .replace(/^([^\n|]+?)(\|[^\n]*\|)(?=\n\|?[ :|-]+\|)/gm, '$1\n$2')
    // 标题行里粘了正文粗体: "### 小标题**法律定性：**" → 标题与正文分行
    .replace(/^(#{1,6} [^\n*]{6,}?)(\*\*)/gm, '$1\n\n$2')
    // 标题行里粘了整句正文: "## 七、把话说明白。你现在…" → 句号后断行
    .replace(/^(#{1,6} [^\n]{2,40}?[。！？])([^\n。！？].*)$/gm, '$1\n\n$2')
  return padCjkBold(prepared)
}

const html = computed(() => md.render(normalizeMarkdown(props.text)))
</script>

<template>
  <div id="markdown-view" class="markdown-view md-body" v-html="html" />
</template>
