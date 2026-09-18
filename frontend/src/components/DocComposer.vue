<script setup>
import { ref } from 'vue'
import { state } from '../store'
import ShimmerButton from './inspira/ShimmerButton.vue'

const emit = defineEmits(['submit'])
const text = ref('')
const docType = ref('complaint') // complaint 起诉状 | defense 答辩状

function send() {
  const t = text.value.trim()
  if (!t || state.value.busy) return
  emit('submit', {
    text: t,
    docType: state.value.mode === 'assistant' ? docType.value : '',
  })
  text.value = ''
}
</script>

<template>
  <div class="flex flex-col gap-2">
    <template v-if="state.mode === 'assistant'">
      <div class="flex gap-4 text-sm">
        <label class="flex items-center gap-1 cursor-pointer">
          <input v-model="docType" type="radio" value="complaint" />起诉状
        </label>
        <label class="flex items-center gap-1 cursor-pointer">
          <input v-model="docType" type="radio" value="defense" />答辩状
        </label>
      </div>
      <textarea
        v-model="text"
        rows="4"
        class="border rounded-xl px-3 py-2 text-sm w-full resize-y focus:outline-none focus:ring-2 focus:ring-slate-400"
        placeholder="粘贴完整案情(至少 20 字), 将按所选文书类型起草"
        @keydown.ctrl.enter="send"
      ></textarea>
    </template>
    <input
      v-else
      v-model="text"
      class="border rounded-xl px-3 py-2 text-sm w-full focus:outline-none focus:ring-2 focus:ring-slate-400"
      placeholder="输入法律咨询问题, 回车发送"
      maxlength="4000"
      @keyup.enter="send"
    />
    <div class="flex justify-end">
      <ShimmerButton :disabled="state.busy" @click="send">
        {{ state.busy ? '处理中…' : '发送' }}
      </ShimmerButton>
    </div>
  </div>
</template>
