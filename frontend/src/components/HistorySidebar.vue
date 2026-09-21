<script setup>
import { onMounted, ref, watch } from 'vue'
import { setSession, sessionsTick } from '../store'
import { listSessions, getSessionDetail } from '../api'
import CitationList from './CitationList.vue'

const sessions = ref([])
const error = ref('')
const detail = ref(null)
const detailOf = ref('')
const openSeq = ref(0) // M12: 乱序守卫 —— 只认最新一次点击的响应

// L11: 列表拉取独立成函数, onMounted 与 sessionsTick 刷新共用
async function loadSessions() {
  try {
    sessions.value = await listSessions()
  } catch (e) {
    error.value = String(e)
  }
}

onMounted(loadSessions)

// L11: 新会话建立/流中 session_id 事件后刷新列表(不加轮询)
watch(sessionsTick, loadSessions)

// M12: 旧实现裸 await 无 try/catch(fetch 失败未提示)、慢响应乱序覆盖、
// setSession 在 fetch 前提交(点了会话切走后仍被切回)——
// 失败写 error 并显示; 只认最新响应; setSession 移到详情拉取成功后
async function open(sid) {
  if (detailOf.value === sid) {
    detail.value = null
    detailOf.value = ''
    return
  }
  const seq = ++openSeq.value
  error.value = ''
  try {
    const d = await getSessionDetail(sid)
    if (seq !== openSeq.value) return // 已有更新的点击, 丢弃本次过期响应
    detail.value = d
    detailOf.value = sid
    setSession(sid)
    // L11: 切会话后刷新列表(切换侧 last_active 排序变化)
    sessionsTick.value++
  } catch (e) {
    error.value = String(e)
  }
}

// L9: 列表源 filter(Boolean) 防后端降级/脏数据混入 null 项
function citationsOf(d) {
  if (!d) return []
  return [...(d.law_results || []), ...(d.rag_documents || [])].filter(Boolean)
}
</script>

<template>
  <aside class="p-3 text-xs overflow-y-auto">
    <div class="font-semibold text-slate-700 mb-2 text-sm">最近会话</div>
    <p v-if="error" class="text-red-500 leading-5">{{ error }}</p>
    <p v-else-if="!sessions.length" class="text-slate-400">暂无历史</p>
    <ul v-else class="space-y-1">
      <li v-for="s in sessions" :key="s.session_id">
        <button
          class="w-full text-left px-2 py-1 rounded hover:bg-slate-100 font-mono truncate"
          :class="detailOf === s.session_id ? 'bg-slate-100' : ''"
          @click="open(s.session_id)"
        >
          {{ s.session_id }}
        </button>
      </li>
    </ul>
    <div v-if="detail" class="mt-3 border rounded-lg p-2 bg-slate-50 leading-5">
      <div class="text-slate-500 mb-1">会话回看</div>
      <div v-if="detail.final_answer" class="whitespace-pre-wrap max-h-64 overflow-y-auto">
        {{ detail.final_answer }}
      </div>
      <div v-else class="text-slate-400">未产出最终回答(可能停在人工确认)</div>
      <CitationList :sources="citationsOf(detail)" />
    </div>
  </aside>
</template>
