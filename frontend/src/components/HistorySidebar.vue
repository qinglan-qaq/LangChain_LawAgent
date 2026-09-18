<script setup>
import { onMounted, ref } from 'vue'
import { setSession } from '../store'
import { listSessions, getSessionDetail } from '../api'
import CitationList from './CitationList.vue'

const sessions = ref([])
const error = ref('')
const detail = ref(null)
const detailOf = ref('')

onMounted(async () => {
  try {
    sessions.value = await listSessions()
  } catch (e) {
    error.value = String(e)
  }
})

async function open(sid) {
  setSession(sid)
  if (detailOf.value === sid) {
    detail.value = null
    detailOf.value = ''
    return
  }
  detail.value = await getSessionDetail(sid)
  detailOf.value = sid
}

function citationsOf(d) {
  if (!d) return []
  return [...(d.law_results || []), ...(d.rag_documents || [])]
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
