<script setup>
import { ref, computed } from 'vue'
import { state } from '../store'
import ShimmerButton from './inspira/ShimmerButton.vue'

// 六类 interrupt 的精确字符串(与后端 normalize_resume 对齐, 不可改动);
// v4: 选项改为后端载荷下发 [{value,label}], 本表仅存显示标签
const TYPE_LABEL = {
  risk_confirm: '高风险确认',
  clarify: '案情澄清',
  mid_clarify: '补充追问',
  pdf_confirm: 'PDF 导出',
  degrade_confirm: '工具降级',
  budget_confirm: '预算确认',
}
// 开放文本型(要素反问/检索追问): 保留自由输入
const TEXT_KINDS = new Set(['clarify', 'mid_clarify'])
// 跳过按钮语义值: 后端归一化可直接识别; 文本型传空串=跳过反问
const PASS_VALUE = {
  risk_confirm: '跳过',
  pdf_confirm: '跳过',
  degrade_confirm: '跳过',
  budget_confirm: '收尾',
}

const props = defineProps({ interrupt: { type: Object, required: true } })
// 流式 resume 由 App.vue 接管(事件路由/气泡更新), 本组件只负责收集答案;
// busy 挂全局 store.busy(App 流程结束统一复位)
const emit = defineEmits(['resume'])

const options = computed(() =>
  (props.interrupt.options || []).map((o) =>
    typeof o === 'string' ? { value: o, label: o } : o
  )
)
const isText = computed(() => TEXT_KINDS.has(props.interrupt.type))
const selected = ref('')
const custom = ref('')
const busy = computed(() => state.value.busy)

function send(answer, allowEmpty = false, e) {
  // IME 组合中(上屏候选词 Enter 在 keyup 时 isComposing 已为 false, 故改 keydown 判 isComposing/keyCode 229): 不提交
  if (e && (e.isComposing || e.keyCode === 229)) return
  if (busy.value) return
  // 文本型空串仅允许跳过路径(PASS_VALUE 传空=跳过反问, 后端按未补充处理)
  if (!allowEmpty && isText.value && !answer) return
  emit('resume', answer)
}

// 确认: 发送选中的选项值(未选时取首项)
function onConfirm() {
  send(selected.value || options.value[0]?.value || '确认')
}

// 跳过: 按类型发语义跳过值(确认类"跳过"/预算"收尾"/文本型空串=跳过反问)
function onPass() {
  send(PASS_VALUE[props.interrupt.type] ?? '', true)
}
</script>

<template>
  <div class="border border-amber-300 rounded-xl p-4 my-2 bg-amber-50">
    <div class="text-sm font-semibold text-amber-800">
      {{ TYPE_LABEL[interrupt.type] || interrupt.type }}
    </div>
    <p class="text-sm my-2">
      {{ interrupt.message || interrupt.question || '请补充信息' }}
    </p>

    <!-- 单选选项(后端 options 载荷, v4): 官方 Inspira 未移植 radio 组件, 按
         ToolTimeline 策略用原生 input + Tailwind 直写, 选中态高亮 -->
    <ul v-if="options.length" class="flex flex-col gap-1 my-2">
      <li v-for="o in options" :key="o.value">
        <label
          class="flex items-center gap-2 px-3 py-1.5 rounded-lg border cursor-pointer text-sm transition-colors"
          :class="
            selected === o.value
              ? 'border-amber-500 bg-amber-100'
              : 'border-slate-300 bg-white hover:bg-amber-50'
          "
        >
          <input v-model="selected" type="radio" class="accent-amber-600" :value="o.value" />
          {{ o.label }}
        </label>
      </li>
    </ul>

    <!-- 选择型: ShimmerButton 确认 + 跳过; 文本型: 自由输入 + 发送 + 跳过 -->
    <div v-if="!isText" class="flex items-center gap-3 my-2">
      <ShimmerButton
        :disabled="busy"
        class="text-sm px-4 py-1.5"
        background="rgba(180,83,9,1)"
        @click="onConfirm"
      >
        确认
      </ShimmerButton>
      <button
        class="px-4 py-1.5 rounded border border-slate-400 text-sm text-slate-600 hover:bg-amber-100"
        :disabled="busy"
        @click="onPass"
      >
        跳过
      </button>
    </div>
    <div v-else class="flex gap-2">
      <input
        v-model="custom"
        class="flex-1 border rounded px-2 py-1 text-sm"
        placeholder="直接输入你的回答/补充内容"
        @keydown.enter="send(custom, false, $event)"
      />
      <ShimmerButton
        :disabled="busy || !custom"
        class="text-sm px-4 py-1.5"
        background="rgba(180,83,9,1)"
        @click="send(custom)"
      >
        发送
      </ShimmerButton>
      <button
        class="px-4 py-1.5 rounded border border-slate-400 text-sm text-slate-600 hover:bg-amber-100"
        :disabled="busy"
        @click="onPass"
      >
        跳过
      </button>
    </div>
  </div>
</template>
