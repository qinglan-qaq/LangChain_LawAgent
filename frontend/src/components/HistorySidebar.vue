<script setup>
import { onMounted, ref, watch } from 'vue'
import { setSession, sessionsTick, newSession } from '../store'
import { listSessions, getSessionDetail } from '../api'
import { Plus } from 'lucide-vue-next'
import SessionDrawer from './SessionDrawer.vue'

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

// 任务3: 新建会话(清空当前会话, 下次提问由后端发新 session_id 建新会话)
function onCreate() {
  newSession()
  detail.value = null
}
</script>

<template>
  <aside id="history" class="p-3 text-xs overflow-y-auto">
    <div class="font-semibold text-slate-700 mb-2 text-sm">最近会话</div>
    <button
      id="btn-new-session"
      class="w-full flex items-center gap-1.5 border rounded-lg px-2 py-1.5 text-xs bg-white hover:bg-slate-100 mb-2"
      @click="onCreate"
    >
      <Plus class="w-3.5 h-3.5" />新建会话
    </button>
    <p v-if="error" class="text-red-500 leading-5">{{ error }}</p>
    <p v-else-if="!sessions.length" class="text-slate-400">暂无历史</p>
    <ul v-else id="history-list" class="space-y-1">
      <li v-for="s in sessions" :key="s.session_id">
        <button
          :id="'history-item-' + s.session_id"
          class="w-full text-left px-2 py-1 rounded hover:bg-slate-100 font-mono truncate"
          :class="detailOf === s.session_id ? 'bg-slate-100' : ''"
          @click="open(s.session_id)"
        >
          {{ s.session_id }}
        </button>
      </li>
    </ul>
    <!-- 任务3.5: 原内联详情块改为右侧抽屉(点击遮罩/X/Esc 关闭) -->
    <SessionDrawer :detail="detail" :open="!!detail" @close="detail = null" />
  </aside>
</template>
