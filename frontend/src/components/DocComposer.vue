<script setup>
import { nextTick, ref, watch } from 'vue'
import { state } from '../store'
import ShimmerButton from './inspira/ShimmerButton.vue'

const emit = defineEmits(['submit'])
const text = ref('')
const docType = ref('complaint') // complaint 起诉状 | defense 答辩状
const inputEl = ref(null) // 自动增高锚点(attorney 单行框)

const DOC_TYPES = [
  { value: 'complaint', label: '起诉状' },
  { value: 'defense', label: '答辩状' },
]

// 自动换行+增高: textarea rows=1 起, 长文自动撑高(上限 160px 后内滚)
function autosize() {
  const el = inputEl.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${Math.min(el.scrollHeight, 160)}px`
}

// 发送后清空/恢复模式切换时回缩到单行高
watch(text, () => nextTick(autosize))

function send(e) {
  // IME 组合中(keyup 时 isComposing 已为 false, 故改 keydown 判 isComposing/keyCode 229): 不提交
  if (e && (e.isComposing || e.keyCode === 229)) return
  const t = text.value.trim()
  if (!t || state.value.busy) return
  emit('submit', {
    text: t,
    docType: state.value.mode === 'assistant' ? docType.value : '',
  })
  text.value = ''
}

// Enter 发送, Shift+Enter 换行(textarea 默认行为)
function onEnter(e) {
  if (e.isComposing || e.keyCode === 229) return
  if (e.shiftKey) return
  e.preventDefault()
  send(e)
}
</script>

<template>
  <!-- 需求5: 卡片化提交区 — focus-within 高亮; attorney 输入+发送同一行;
       assistant 文书单选 pill 化 + 案情粘贴 + hint 行 -->
  <div
    id="doc-composer"
    class="doc-composer rounded-2xl border border-slate-200 bg-white p-2.5 shadow-sm transition-shadow focus-within:ring-2 focus-within:ring-slate-300"
  >
    <template v-if="state.mode === 'assistant'">
      <div id="doc-type-pills" class="doc-type-pills mb-2 flex items-center gap-2">
        <button
          v-for="dt in DOC_TYPES"
          :id="'doc-type-' + dt.value"
          :key="dt.value"
          type="button"
          class="rounded-full px-3 py-1 text-xs border transition-colors"
          :class="
            docType === dt.value
              ? 'bg-slate-900 text-white border-slate-900'
              : 'bg-white text-slate-600 border-slate-300 hover:border-slate-400'
          "
          @click="docType = dt.value"
        >
          {{ dt.label }}
        </button>
        <span class="ml-auto text-[10px] text-slate-400">按所选类型起草文书</span>
      </div>
      <textarea
        id="input"
        v-model="text"
        rows="4"
        class="w-full resize-y rounded-xl px-3 py-2 text-sm focus:outline-none"
        placeholder="粘贴完整案情(至少 20 字), 将按所选文书类型起草"
        maxlength="4000"
        @keydown.ctrl.enter="send"
      ></textarea>
      <div class="composer-footer mt-1.5 flex items-center gap-2">
        <span class="composer-hint text-[10px] text-slate-400">Ctrl + Enter 发送 · 最多 4000 字</span>
        <div class="ml-auto">
          <ShimmerButton :disabled="state.busy" @click="send">
            {{ state.busy ? '处理中…' : '发送' }}
          </ShimmerButton>
        </div>
      </div>
    </template>
    <template v-else>
      <div class="flex items-end gap-2">
        <textarea
          id="input"
          ref="inputEl"
          v-model="text"
          rows="1"
          class="flex-1 resize-none rounded-xl px-3 py-2 text-sm leading-6 focus:outline-none max-h-40 overflow-y-auto"
          placeholder="输入法律咨询问题, 回车发送"
          maxlength="4000"
          @keydown.enter="onEnter"
        ></textarea>
        <ShimmerButton :disabled="state.busy" @click="send">
          {{ state.busy ? '处理中…' : '发送' }}
        </ShimmerButton>
      </div>
      <div class="composer-hint mt-1 px-1 text-[10px] text-slate-400">
        Enter 发送 · Shift+Enter 换行 · 最多 4000 字
      </div>
    </template>
  </div>
</template>
