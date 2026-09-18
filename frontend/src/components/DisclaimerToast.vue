<script setup>
import { onMounted, ref } from 'vue'
import { state, markDisclaimerShown } from '../store'
import { fetchDisclaimer } from '../api'

const text = ref('')
onMounted(async () => {
  try {
    text.value = await fetchDisclaimer()
  } catch {
    /* 免责弹窗非阻塞(用户决策): 拉取失败也不影响主流程, 下次进入再拉 */
    text.value = '本工具生成的所有内容仅供参考，不构成正式法律意见。'
  }
})
</script>

<template>
  <div
    v-if="!state.disclaimerShown"
    class="fixed top-4 right-4 max-w-xs z-50 border rounded-xl bg-white shadow-lg p-3 text-xs text-slate-600"
  >
    <div class="font-semibold text-slate-800 mb-1">免责声明</div>
    <p class="leading-5">{{ text }}</p>
    <button
      class="mt-2 px-2 py-0.5 rounded bg-slate-900 text-white"
      @click="markDisclaimerShown()"
    >
      我知道了
    </button>
  </div>
</template>
