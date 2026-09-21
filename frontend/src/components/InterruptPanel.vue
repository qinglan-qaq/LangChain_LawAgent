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
// 选择题模式(需求1): 文本型 + 后端下发非空 options → 单选 + 「其他」补充输入
const isMCQ = computed(() => isText.value && options.value.length > 0)
const selected = ref('')
const custom = ref('')
const otherText = ref('') // 「其他」选项的补充输入
const busy = computed(() => state.value.busy)

// 「其他」选项哨兵值: 与后端下发选项 value 空间隔离
const OTHER = '__other__'
// 选择题选项(含追加的「其他」项)
const mcqOptions = computed(() => [...options.value, { value: OTHER, label: '其他（请输入）' }])
// 选项字母徽标: 优先后端 value(单字符 A/B/C...), 缺失按序号补(65+i 字符码)
function letterOf(i, o) {
  const v = String(o.value || '')
  return v.length === 1 ? v : String.fromCharCode(65 + i)
}
// 选择题可提交: 已选项且选「其他」时补充输入非空
const canSubmitMCQ = computed(
  () => selected.value && (selected.value !== OTHER || otherText.value.trim()),
)
// 选择题提交: 载荷取选项的 label 文本(非字母/哨兵值), 「其他」取补充输入
function onMCQSubmit() {
  if (busy.value || !canSubmitMCQ.value) return
  if (selected.value === OTHER) emit('resume', otherText.value.trim())
  else emit('resume', options.value.find((o) => o.value === selected.value)?.label || selected.value)
}
// 「其他」输入框 Enter 提交(IME 组合中不提交, 同 send 的 keydown 判定)
function onOtherEnter(e) {
  if (e.isComposing || e.keyCode === 229) return
  onMCQSubmit()
}

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
    <div class="flex items-center gap-2">
      <div class="text-sm font-semibold text-amber-800">
        {{ TYPE_LABEL[interrupt.type] || interrupt.type }}
      </div>
      <!-- 轮次徽标: 多轮澄清时后端下发 round(旧载荷无此键则不显示) -->
      <span
        v-if="interrupt.round"
        class="text-xs text-amber-700 bg-amber-100 rounded px-1.5 py-0.5"
      >
        第 {{ interrupt.round }} 轮
      </span>
    </div>
    <p class="text-sm my-2">
      {{ interrupt.question || interrupt.message || '请补充信息' }}
    </p>

    <!-- 选择题模式(需求1): 文本型 + options 载荷 → 单选(字母徽标) + 「其他」补充输入;
         样式沿原生 input + Tailwind 直写(官方 Inspira 无 radio 组件) -->
    <template v-if="isMCQ">
      <ul class="flex flex-col gap-1 my-2">
        <li v-for="(o, i) in mcqOptions" :key="o.value">
          <label
            class="flex items-center gap-2 px-3 py-1.5 rounded-lg border cursor-pointer text-sm transition-colors"
            :class="
              selected === o.value
                ? 'border-amber-500 bg-amber-100'
                : 'border-slate-300 bg-white hover:bg-amber-50'
            "
          >
            <input v-model="selected" type="radio" class="accent-amber-600" :value="o.value" />
            <span
              class="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded border border-amber-400 bg-amber-50 text-xs font-semibold text-amber-700"
            >
              {{ letterOf(i, o) }}
            </span>
            {{ o.label }}
          </label>
        </li>
      </ul>
      <!-- 「其他」选中后展开的补充输入(IME 组合中的 Enter 不提交) -->
      <input
        v-if="selected === OTHER"
        v-model="otherText"
        class="w-full border rounded px-2 py-1 text-sm my-1"
        placeholder="请输入其他内容"
        @keydown.enter="onOtherEnter"
      />
      <div class="flex items-center gap-3 my-2">
        <ShimmerButton
          :disabled="busy || !canSubmitMCQ"
          class="text-sm px-4 py-1.5"
          background="rgba(180,83,9,1)"
          @click="onMCQSubmit"
        >
          提交
        </ShimmerButton>
        <button
          class="px-4 py-1.5 rounded border border-slate-400 text-sm text-slate-600 hover:bg-amber-100"
          :disabled="busy"
          @click="onPass"
        >
          跳过
        </button>
      </div>
    </template>

    <!-- 确认型选项(后端 options 载荷, v4): 选中态高亮 -->
    <ul v-else-if="options.length" class="flex flex-col gap-1 my-2">
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

    <!-- 选择型: ShimmerButton 确认 + 跳过; 文本型(无选项): 自由输入 + 发送 + 跳过 -->
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
    <div v-else-if="!isMCQ" class="flex gap-2">
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
