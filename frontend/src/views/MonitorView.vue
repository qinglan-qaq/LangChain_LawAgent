<!-- MonitorView.vue
  监控页(D-spec §六): 总览卡 → runs 表(分页) → 侧拉运行详情页(甘特+阶段抽屉);
  3s 轮询, 无 running 行停轮。所有框带唯一 id + 语义 class(便于定位描述)。
-->
<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import {
  getMonitorEvals, getMonitorOverview, getMonitorRunDetail, getMonitorRuns,
} from '../api'
import GanttTimeline from '../components/GanttTimeline.vue'
import StageDetailDrawer from '../components/StageDetailDrawer.vue'
import { fmtCST } from '../lib/time'

const PAGE_SIZE = 20

const overview = ref({})
const runs = ref([])
const totalRuns = ref(0)
const page = ref(1)
const evals = ref([])
const detail = ref(null)          // MonitorRunDetail(侧拉页数据)
const selectedStage = ref(null)   // MonitorStage
const panelShown = ref(false)     // 侧拉页滑入态(手动 class 切换, 不依赖 Vue Transition 的 rAF)
const timer = ref(null)
const now = ref(Date.now())

const maxPage = computed(() => Math.max(1, Math.ceil(totalRuns.value / PAGE_SIZE)))

async function fetchRuns() {
  const { rows, total } = await getMonitorRuns(PAGE_SIZE, (page.value - 1) * PAGE_SIZE)
  runs.value = rows
  totalRuns.value = total
  return rows
}

function anyRunning(rs) {
  return (rs || runs.value).some((r) => r.stage_running > 0)
}

async function refresh() {
  const [ov, ev, rs] = await Promise.all([
    getMonitorOverview(), getMonitorEvals(), fetchRuns(),
  ])
  overview.value = ov
  evals.value = ev
  now.value = Date.now()
  if (anyRunning(rs) && !timer.value) {
    timer.value = setInterval(tick, 3000)
  } else if (!anyRunning(rs) && timer.value) {
    clearInterval(timer.value)
    timer.value = null
  }
}

async function tick() {
  await fetchRuns()
  overview.value = await getMonitorOverview()
  if (detail.value) {
    detail.value = await getMonitorRunDetail(detail.value.run_id)
  }
  now.value = Date.now()
}

function goPage(p) {
  const target = Math.min(Math.max(p, 1), maxPage.value)
  if (target === page.value) return
  page.value = target
  fetchRuns()
}

async function openRun(r) {
  detail.value = await getMonitorRunDetail(r.run_id)
  selectedStage.value = null
  panelShown.value = false
  await nextTick()               // 先以滑出态挂载, 下一帧切 translate-x-0 触发过渡
  panelShown.value = true
}

function closeRun() {
  panelShown.value = false
  setTimeout(() => {              // 等滑出动画走完再卸载
    detail.value = null
    selectedStage.value = null
  }, 260)
}

function scoreCls(s) {
  if (s == null) return 'text-slate-400'
  return s >= 80 ? 'text-emerald-600' : s >= 60 ? 'text-amber-600' : 'text-red-500'
}

onMounted(refresh)
onUnmounted(() => timer.value && clearInterval(timer.value))
</script>

<template>
  <div id="monitor-page" class="monitor-page h-screen overflow-y-auto bg-slate-100 p-4 space-y-4">
    <header id="monitor-header" class="monitor-header flex items-center gap-3 border-b pb-2 bg-white/80 p-3 rounded">
      <router-link id="monitor-back-link" to="/" class="monitor-back-link text-sm text-blue-600 hover:underline">← 返回咨询</router-link>
      <h1 id="monitor-title" class="monitor-title text-lg font-semibold text-slate-700">Agent 阶段监控</h1>
      <span v-if="overview.running_stages" id="monitor-running-badge"
            class="monitor-running-badge text-xs text-blue-600">
        {{ overview.running_stages }} 个阶段进行中
      </span>
    </header>

    <!-- ① 总览卡 -->
    <section id="monitor-overview" class="monitor-overview grid grid-cols-2 md:grid-cols-4 gap-3">
      <div id="monitor-card-runs" class="monitor-card border rounded bg-white p-3 text-sm">
        <p class="text-slate-400 text-xs">近24h 运行</p>
        <p class="text-xl font-semibold text-slate-700">
          {{ Object.values(overview.runs_by_status || {}).reduce((a, b) => a + b, 0) }}
        </p>
        <p id="monitor-runs-by-status" class="text-xs text-slate-500">{{ overview.runs_by_status }}</p>
      </div>
      <div id="monitor-card-running" class="monitor-card border rounded bg-white p-3 text-sm">
        <p class="text-slate-400 text-xs">进行中阶段</p>
        <p class="text-xl font-semibold text-blue-600">{{ overview.running_stages ?? 0 }}</p>
      </div>
      <div id="monitor-card-limit-hit" class="monitor-card border rounded bg-white p-3 text-sm">
        <p class="text-slate-400 text-xs">触顶运行(limit_hit)</p>
        <p class="text-xl font-semibold" :class="overview.limit_hit_runs ? 'text-red-500' : 'text-slate-700'">
          {{ overview.limit_hit_runs ?? 0 }}
        </p>
      </div>
      <div id="monitor-card-fail-top" class="monitor-card border rounded bg-white p-3 text-sm">
        <p class="text-slate-400 text-xs">节点失败排行(24h)</p>
        <p v-for="f in overview.node_fail_top || []" :key="f.node_name"
           class="text-xs text-slate-600">{{ f.node_name }}: {{ f.errors }}</p>
        <p v-if="!(overview.node_fail_top || []).length" class="text-xs text-slate-400">无</p>
      </div>
    </section>

    <!-- ② 指标看板卡(评测批次) -->
    <section id="monitor-evals" class="monitor-evals border rounded bg-white p-3">
      <h2 id="monitor-evals-title" class="monitor-evals-title text-sm font-semibold text-slate-600 mb-2">评测批次</h2>
      <table id="monitor-evals-table" class="monitor-evals-table w-full text-xs" v-if="evals.length">
        <thead id="monitor-evals-thead" class="text-slate-400 text-left">
          <tr><th class="py-1">时间</th><th>数据集</th><th>标签</th>
              <th>hit_rate@5</th><th>MRR@5</th><th>P@5</th><th>R@5</th><th>F1@5</th></tr>
        </thead>
        <tbody class="text-slate-600">
          <tr v-for="e in evals" :key="e.id" :id="`monitor-eval-row-${e.id}`"
              class="monitor-eval-row border-t">
            <td class="py-1">{{ fmtCST(e.created_at) }}</td>
            <td>{{ e.dataset }}</td><td>{{ e.label }}</td>
            <td>{{ (e.metrics['hit_rate_at_5'] ?? 0).toFixed(3) }}</td>
            <td>{{ (e.metrics['mrr_at_5'] ?? 0).toFixed(3) }}</td>
            <td>{{ (e.metrics['precision_at_5'] ?? 0).toFixed(3) }}</td>
            <td>{{ (e.metrics['recall_at_5'] ?? 0).toFixed(3) }}</td>
            <td>{{ (e.metrics['f1_at_5'] ?? 0).toFixed(3) }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="text-xs text-slate-400">暂无批次 — python scripts/run_eval.py --suite retrieval --label &lt;sha&gt;</p>
    </section>

    <!-- ③ runs 列表(分页) -->
    <section id="monitor-runs" class="monitor-runs border rounded bg-white p-3">
      <h2 id="monitor-runs-title" class="monitor-runs-title text-sm font-semibold text-slate-600 mb-2">
        运行列表(共{{ totalRuns }})
      </h2>
      <table id="monitor-runs-table" class="monitor-runs-table w-full text-xs" v-if="runs.length">
        <thead id="monitor-runs-thead" class="text-slate-400 text-left">
          <tr><th class="py-1">开始时间</th><th>run_id</th><th>会话</th><th>模式</th>
              <th>状态</th><th>阶段(ok/总)</th><th>综合分</th><th>触顶</th></tr>
        </thead>
        <tbody class="text-slate-600">
          <tr v-for="r in runs" :key="r.run_id" :id="`monitor-run-row-${r.run_id}`"
              class="monitor-run-row border-t cursor-pointer hover:bg-slate-50"
              :class="{ 'bg-blue-50/50': detail && detail.run_id === r.run_id }"
              @click="openRun(r)">
            <td class="py-1">{{ fmtCST(r.started_at).slice(5) }}</td>
            <td class="max-w-40 truncate">{{ r.run_id }}</td>
            <td class="max-w-32 truncate">{{ r.session_id }}</td>
            <td>{{ r.mode || r.run_type }}</td>
            <td>
              <span :class="{
                ok: 'text-emerald-600', error: 'text-red-500',
                interrupted: 'text-amber-600',
              }[r.status] || 'text-slate-500'">{{ r.status }}</span>
            </td>
            <td>{{ r.stage_ok }}/{{ r.stage_total }}
              <span v-if="r.stage_running" class="text-blue-500">(进行中{{ r.stage_running }})</span>
            </td>
            <td :class="scoreCls(r.metrics?.composite_score)">
              {{ r.metrics?.composite_score ?? '—' }}
            </td>
            <td :class="r.metrics?.limit_hit ? 'text-red-500' : 'text-slate-400'">
              {{ r.metrics?.limit_hit ? '是' : '—' }}
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else class="text-xs text-slate-400">暂无运行数据</p>

      <!-- 翻页控件 -->
      <div id="monitor-runs-pagination" class="monitor-runs-pagination mt-2 flex items-center justify-end gap-2 text-xs">
        <button id="monitor-page-first" class="monitor-page-btn border rounded px-2 py-0.5 hover:bg-slate-50 disabled:opacity-40"
                :disabled="page <= 1" @click="goPage(1)">«</button>
        <button id="monitor-page-prev" class="monitor-page-btn border rounded px-2 py-0.5 hover:bg-slate-50 disabled:opacity-40"
                :disabled="page <= 1" @click="goPage(page - 1)">上一页</button>
        <span id="monitor-page-info" class="monitor-page-info text-slate-500">
          第 {{ page }} / {{ maxPage }} 页 · 每页 {{ PAGE_SIZE }} 条
        </span>
        <button id="monitor-page-next" class="monitor-page-btn border rounded px-2 py-0.5 hover:bg-slate-50 disabled:opacity-40"
                :disabled="page >= maxPage" @click="goPage(page + 1)">下一页</button>
        <button id="monitor-page-last" class="monitor-page-btn border rounded px-2 py-0.5 hover:bg-slate-50 disabled:opacity-40"
                :disabled="page >= maxPage" @click="goPage(maxPage)">»</button>
      </div>
    </section>

    <!-- ④ 运行详情侧拉页(蒙层 + 右侧滑出) -->
    <Teleport to="body">
      <div v-if="detail" id="monitor-detail-mask"
           class="monitor-detail-mask fixed inset-0 bg-black/30 z-30 transition-opacity duration-200"
           :class="panelShown ? 'opacity-100' : 'opacity-0'"
           @click="closeRun" />
      <aside v-if="detail" id="monitor-detail-panel"
             class="monitor-detail-panel fixed inset-y-0 right-0 w-full md:w-3/4 xl:w-3/5 bg-slate-100 shadow-2xl z-40 overflow-y-auto p-4 space-y-4 transition-transform duration-200 ease-out"
             :class="panelShown ? 'translate-x-0' : 'translate-x-full'">
        <header id="monitor-detail-header" class="monitor-detail-header flex items-center gap-3 border-b pb-2 bg-white/80 p-3 rounded">
          <h2 id="monitor-detail-title" class="monitor-detail-title text-base font-semibold text-slate-700 truncate">
            甘特时间线 — {{ detail.run_id }}
          </h2>
          <span class="text-xs text-slate-400">{{ detail.status }} · {{ detail.stage_ok }}/{{ detail.stage_total }} 阶段</span>
          <button id="monitor-detail-close" class="monitor-detail-close ml-auto text-sm text-slate-500 hover:text-slate-700 border rounded px-2 py-0.5"
                  @click="closeRun">✕ 关闭</button>
        </header>
        <GanttTimeline :stages="detail.stages" :spans="detail.spans" :now="now"
                       @select="selectedStage = $event" />
        <StageDetailDrawer :stage="selectedStage" :spans="detail.spans" />
      </aside>
    </Teleport>
  </div>
</template>
