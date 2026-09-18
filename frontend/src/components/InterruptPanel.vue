<script setup>
import { ref } from 'vue'
import { state, setSession } from '../store'
import { resumeHITL as callResume } from '../api'

// 六类 interrupt 的精确字符串(与后端 normalize_resume 对齐, 不可改动)
const TYPE_UI = {
  risk_confirm: { kind: 'confirm', label: '高风险确认' },
  clarify: { kind: 'text', label: '案情澄清' },
  mid_clarify: { kind: 'text', label: '补充追问' },
  pdf_confirm: { kind: 'confirm', label: 'PDF 导出' },
  degrade_confirm: { kind: 'choice', label: '工具降级', options: ['重试', '跳过', '终止'] },
  budget_confirm: { kind: 'choice', label: '预算确认', options: ['收尾'] },
}

const props = defineProps({ interrupt: { type: Object, required: true } })
const emit = defineEmits(['resumed'])

const custom = ref('')
const busy = ref(false)

async function send(answer) {
  if (!answer || busy.value) return
  busy.value = true
  const r = await callResume(props.interrupt.session_id || state.value.sessionId, answer)
  busy.value = false
  setSession(r.session_id)
  emit('resumed', r)
}
</script>

<template>
  <div class="border border-amber-300 rounded-xl p-4 my-2 bg-amber-50">
    <div class="text-sm font-semibold text-amber-800">
      {{ TYPE_UI[interrupt.type]?.label || interrupt.type }}
    </div>
    <p class="text-sm my-2">
      {{ interrupt.message || interrupt.question || '请补充信息' }}
    </p>
    <div class="flex gap-2 flex-wrap my-2">
      <button
        v-if="TYPE_UI[interrupt.type]?.kind === 'confirm'"
        class="px-3 py-1 rounded bg-amber-600 text-white"
        @click="send('是')"
      >
        继续
      </button>
      <button
        v-if="TYPE_UI[interrupt.type]?.kind === 'confirm'"
        class="px-3 py-1 rounded border"
        @click="send('否')"
      >
        中止
      </button>
      <button
        v-for="o in TYPE_UI[interrupt.type]?.options || []"
        :key="o"
        class="px-3 py-1 rounded border"
        @click="send(o)"
      >
        {{ o }}
      </button>
    </div>
    <div class="flex gap-2">
      <input
        v-model="custom"
        class="flex-1 border rounded px-2 py-1 text-sm"
        placeholder="自助输入: 直接输入你的回答/补充内容"
        @keyup.enter="send(custom)"
      />
      <button
        class="px-3 py-1 rounded bg-amber-600 text-white text-sm"
        :disabled="busy"
        @click="send(custom)"
      >
        发送
      </button>
    </div>
  </div>
</template>
