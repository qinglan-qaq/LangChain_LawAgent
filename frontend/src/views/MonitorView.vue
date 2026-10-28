<!-- MonitorView.vue
  监控页(v2): 总览卡 → 评测批次 → runs 列表 → 选中 run 的垂直时间轴
  (链式串联 HITL 续跑) + 时间轴下方阶段详情面板。
  2s 增量轮询 minor-states, 出现新 stage 再刷新时间轴; 停轮条件 = 无运行中节点。
  深链: /monitor#run=<run_id>&stage=<stage_id>。
-->
<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import {
  getMonitorConfig, getMonitorEvals, getMonitorMinorStates, getMonitorOverview,
  getMonitorRuns, getMonitorStageDetail, getMonitorTimeline,
} from '../api'
import MinorStateDetail from '../components/MinorStateDetail.vue'
import MonitorTimeline from '../components/MonitorTimeline.vue'
import { fmtCST } from '../lib/time'

const PAGE_SIZE = 20
const POLL_MS = 2000

const overview = ref({})
const runs = ref([])
const totalRuns = ref(0)
const page = ref(1)
const evals = ref([])
const config = ref({})

const selectedRunId = ref('')
const timeline = ref(null)
const timelineLoading = ref(false)
const selectedStageId = ref(null)
const stageDetail = ref(null)
const detailLoading = ref(false)

const phaseFilter = ref('')
const nodeFilter = ref('')
const monitorError = ref('')
const detailSection = ref(null)
const timer = ref(null)
const now = ref(Date.now())

const maxPage = computed(() => Math.max(1, Math.ceil(totalRuns.value / PAGE_SIZE)))
const selectedRun = computed(
  () => runs.value.find((r) => r.run_id === selectedRunId.value) || null
)
const timelineNodes = computed(
  () => (timeline.value?.items || []).filter((x) => x.kind === 'node' && x.state)
)
const nodeOptions = computed(() => {
  const seen = new Set()
  for (const x of timelineNodes.value) seen.add(x.state.node_name)
  return [...seen].sort()
})
const currentStageId = computed(() => {
  const own = timelineNodes.value.filter((x) => x.run_id === selectedRunId.value)
  const last = own[own.length - 1]
  return last?.state?.stage_id ?? null
})

async function fetchRuns() {
  const { rows, total } = await getMonitorRuns(
    PAGE_SIZE,
    (page.value - 1) * PAGE_SIZE
  )
  runs.value = rows
  totalRuns.value = total
  return rows
}

async function loadTimeline() {
  if (!selectedRunId.value) return
  timelineLoading.value = true
  try {
    timeline.value = await getMonitorTimeline(selectedRunId.value, true)
  } catch (e) {
    monitorError.value = e?.response?.data?.detail || '时间轴加载失败'
    timeline.value = null
  } finally {
    timelineLoading.value = false
  }
}

// 返回 { patched, fresh }: fresh>0 表示出现了时间轴里还没有的 stage 行
function patchTimelineStates(rows) {
  if (!timeline.value || !rows?.length) return { patched: 0, fresh: 0 }
  const byId = new Map(rows.map((r) => [r.stage_id, r]))
  let patched = 0
  timeline.value.items = timeline.value.items.map((it) => {
    if (it.kind !== 'node' || !byId.has(it.state?.stage_id)) return it
    patched += 1
    return { ...it, state: byId.get(it.state.stage_id) }
  })
  const known = new Set(
    timeline.value.items.filter((x) => x.kind === 'node').map((x) => x.state.stage_id)
  )
  return { patched, fresh: rows.filter((r) => !known.has(r.stage_id)).length }
}

async function pollSelected() {
  const rid = selectedRunId.value
  if (!rid) return
  try {
    const rows = await getMonitorMinorStates(rid, 0)
    const result = patchTimelineStates(rows)
    if (result.fresh > 0) {
      // 新 stage 行出现(节点重跑/续跑)才重建时间轴结构
      await loadTimeline()
    }
    if (selectedStageId.value) {
      const latest = (timeline.value?.items || []).find(
        (x) => x.kind === 'node' && x.state?.stage_id === selectedStageId.value
      )?.state
      if (latest) {
        stageDetail.value = { ...(stageDetail.value || {}), state: latest }
        if (latest.phase === 'running') {
          stageDetail.value = await getMonitorStageDetail(selectedStageId.value)
        }
      }
    }
  } catch {
    // 轮询失败静默; 下一次 tick 重试
  }
}

async function refresh() {
  try {
    const [ov, ev, rs, cfg] = await Promise.all([
      getMonitorOverview(), getMonitorEvals(), fetchRuns(), getMonitorConfig(),
    ])
    overview.value = ov
    evals.value = ev
    config.value = cfg
    now.value = Date.now()
    await applyHash()
    ensureTimer(rs)
  } catch (e) {
    monitorError.value = e?.response?.data?.detail || '监控数据暂不可用'
  }
}

async function tick() {
  try {
    const [ov, rs] = await Promise.all([getMonitorOverview(), fetchRuns()])
    overview.value = ov
    await pollSelected()
    now.value = Date.now()
    ensureTimer(rs)
  } catch {
    // 临时失败: 保持既有视图, 下个 tick 重试
  }
}

function ensureTimer(rs = runs.value) {
  const any = (rs || []).some((r) => r.status === 'running')
    || timelineNodes.value.some((x) => x.state.phase === 'running')
    || selectedRun.value?.status === 'running'
  if (any && !timer.value) timer.value = setInterval(tick, POLL_MS)
  else if (!any && timer.value) {
    clearInterval(timer.value)
    timer.value = null
  }
}

function stopTimer() {
  if (timer.value) clearInterval(timer.value)
  timer.value = null
}

function hashParams() {
  return new URLSearchParams((window.location.hash || '').replace(/^#/, ''))
}

function syncHash() {
  const p = new URLSearchParams()
  if (selectedRunId.value) p.set('run', selectedRunId.value)
  if (selectedStageId.value) p.set('stage', String(selectedStageId.value))
  const q = p.toString()
  const url = `${window.location.pathname}${q ? `#${q}` : ''}`
  window.history.replaceState(null, '', url)
}

async function applyHash() {
  const p = hashParams()
  const run = p.get('run')
  const stage = p.get('stage')
  if (run && run !== selectedRunId.value) await openRun(run, stage)
  else if (run && stage && Number(stage) !== selectedStageId.value) {
    await selectStage(Number(stage))
  }
}

async function openRun(runId, stageId = null) {
  selectedRunId.value = runId
  selectedStageId.value = null
  stageDetail.value = null
  nodeFilter.value = ''
  phaseFilter.value = ''
  monitorError.value = ''
  await loadTimeline()
  if (stageId) await selectStage(Number(stageId))
  await pollSelected()
  syncHash()
  ensureTimer()
  await nextTick()
  detailSection.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

async function selectStage(stageId) {
  selectedStageId.value = stageId
  detailLoading.value = true
  try {
    stageDetail.value = await getMonitorStageDetail(stageId)
  } catch (e) {
    stageDetail.value = null
    monitorError.value = e?.response?.data?.detail || '阶段详情加载失败'
  } finally {
    detailLoading.value = false
  }
  syncHash()
  await nextTick()
  document.getElementById('minor-state-detail')?.scrollIntoView({
    behavior: 'smooth', block: 'start',
  })
}

function closeRun() {
  stopTimer()
  selectedRunId.value = ''
  selectedStageId.value = null
  timeline.value = null
  stageDetail.value = null
  monitorError.value = ''
  syncHash()
}

function goPage(p) {
  const target = Math.min(Math.max(p, 1), maxPage.value)
  if (target === page.value) return
  page.value = target
  fetchRuns()
}

function scoreCls(s) {
  if (s == null) return 'text-slate-400'
  return s >= 80 ? 'text-emerald-600' : s >= 60 ? 'text-amber-600' : 'text-red-500'
}

function fmtDuration(ms) {
  if (ms == null || isNaN(ms) || ms < 0) return null
  if (ms < 60000) return `${Math.round(ms / 1000)}s`
  return `${(ms / 60000).toFixed(1)}m`
}

async function onHashChange() {
  await applyHash()
}

onMounted(() => {
  window.addEventListener('hashchange', onHashChange)
  refresh()
})
onUnmounted(() => {
  window.removeEventListener('hashchange', onHashChange)
  stopTimer()
})
</script>

<template>
  <div id="monitor-page" class="monitor-page min-h-screen overflow-y-auto bg-slate-100 p-4 space-y-4">
    <header id="monitor-header" class="monitor-header flex flex-wrap items-center gap-3 rounded border-b bg-white/80 p-3">
      <router-link id="monitor-back-link" to="/" class="monitor-back-link text-sm text-blue-600 hover:underline">← 返回咨询</router-link>
      <h1 id="monitor-title" class="monitor-title text-lg font-semibold text-slate-700">Agent 阶段监控</h1>
      <span v-if="overview.running_stages" id="monitor-running-badge"
            class="monitor-running-badge text-xs text-blue-600">
        {{ overview.running_stages }} 个阶段进行中 · {{ overview.running_minor_nodes ?? 0 }} 个节点有内容
      </span>
      <span class="ml-auto text-xs text-slate-400">每 2s 增量刷新</span>
    </header>

    <p v-if="monitorError" id="monitor-error"
       class="rounded border border-red-200 bg-red-50 p-2 text-xs text-red-600">
      {{ monitorError }}
    </p>

    <!-- ① 总览卡 -->
    <section id="monitor-overview" class="monitor-overview grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
      <div id="monitor-card-runs" class="monitor-card rounded border bg-white p-3 text-sm">
        <p class="text-xs text-slate-400">近24h 运行</p>
        <p class="text-xl font-semibold text-slate-700">
          {{ Object.values(overview.runs_by_status || {}).reduce((a, b) => a + b, 0) }}
        </p>
        <p id="monitor-runs-by-status" class="text-xs text-slate-500">{{ overview.runs_by_status }}</p>
      </div>
      <div id="monitor-card-running" class="monitor-card rounded border bg-white p-3 text-sm">
        <p class="text-xs text-slate-400">进行中阶段</p>
        <p class="text-xl font-semibold text-blue-600">{{ overview.running_stages ?? 0 }}</p>
      </div>
      <div id="monitor-card-running-minor" class="monitor-card rounded border bg-white p-3 text-sm">
        <p class="text-xs text-slate-400">运行中内容行</p>
        <p class="text-xl font-semibold text-sky-600">{{ overview.running_minor_nodes ?? 0 }}</p>
      </div>
      <div id="monitor-card-limit-hit" class="monitor-card rounded border bg-white p-3 text-sm">
        <p class="text-xs text-slate-400">触顶运行(limit_hit)</p>
        <p class="text-xl font-semibold" :class="overview.limit_hit_runs ? 'text-red-500' : 'text-slate-700'">
          {{ overview.limit_hit_runs ?? 0 }}
        </p>
      </div>
      <div id="monitor-card-fail-top" class="monitor-card rounded border bg-white p-3 text-sm">
        <p class="text-xs text-slate-400">节点失败排行(24h)</p>
        <p v-for="f in overview.node_fail_top || []" :key="f.node_name"
           class="text-xs text-slate-600">{{ f.node_name }}: {{ f.errors }}</p>
        <p v-if="!(overview.node_fail_top || []).length" class="text-xs text-slate-400">无</p>
      </div>
      <div id="monitor-card-minor-fail-top" class="monitor-card rounded border bg-white p-3 text-sm">
        <p class="text-xs text-slate-400">内容投影失败(24h)</p>
        <p v-for="f in overview.minor_fail_top || []" :key="f.node_name"
           class="text-xs text-slate-600">{{ f.node_name }}: {{ f.errors }}</p>
        <p v-if="!(overview.minor_fail_top || []).length" class="text-xs text-slate-400">无</p>
      </div>
    </section>

    <!-- ② 指标看板卡(评测批次) -->
    <section id="monitor-evals" class="monitor-evals rounded border bg-white p-3">
      <h2 id="monitor-evals-title" class="monitor-evals-title mb-2 text-sm font-semibold text-slate-600">评测批次</h2>
      <table id="monitor-evals-table" class="monitor-evals-table w-full text-xs" v-if="evals.length">
        <thead id="monitor-evals-thead" class="text-left text-slate-400">
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
    <section id="monitor-runs" class="monitor-runs rounded border bg-white p-3">
      <h2 id="monitor-runs-title" class="monitor-runs-title mb-2 text-sm font-semibold text-slate-600">
        运行列表(共{{ totalRuns }})
      </h2>
      <div class="overflow-x-auto">
        <table id="monitor-runs-table" class="monitor-runs-table w-full text-xs" v-if="runs.length">
          <thead id="monitor-runs-thead" class="text-left text-slate-400">
            <tr><th class="py-1">开始时间</th><th>run_id</th><th>会话</th><th>模式</th>
                <th>状态</th><th>当前节点</th><th>阶段(ok/总)</th><th>内容行</th>
                <th>综合分</th><th>运行时长</th></tr>
          </thead>
          <tbody class="text-slate-600">
            <tr v-for="r in runs" :key="r.run_id" :id="`monitor-run-row-${r.run_id}`"
                class="monitor-run-row cursor-pointer border-t hover:bg-slate-50"
                :class="{ 'bg-blue-50/60': selectedRunId === r.run_id }"
                @click="openRun(r.run_id)">
              <td class="py-1">{{ fmtCST(r.started_at).slice(5) }}</td>
              <td class="max-w-40 truncate" :title="r.run_id">
                <span class="font-mono">{{ r.run_id }}</span>
                <span v-if="r.parent_run_id" class="ml-1 rounded bg-indigo-50 px-1 text-[10px] text-indigo-600"
                      :title="`续跑自 ${r.parent_run_id}`">续</span>
              </td>
              <td class="max-w-32 truncate">{{ r.session_id }}</td>
              <td>{{ r.mode || r.run_type }}</td>
              <td>
                <span :class="{
                  ok: 'text-emerald-600', error: 'text-red-500',
                  interrupted: 'text-amber-600', running: 'text-blue-600',
                }[r.status] || 'text-slate-500'">{{ r.status }}</span>
                <span v-if="r.stale" class="ml-1 rounded bg-amber-50 px-1 text-[10px] text-amber-700"
                      title="running 超过 2 小时, 可能已失联">stale</span>
              </td>
              <td class="max-w-28 truncate text-slate-500">{{ r.current_node || '—' }}</td>
              <td>{{ r.stage_ok }}/{{ r.stage_total }}
                <span v-if="r.stage_running" class="text-blue-500">(进行中{{ r.stage_running }})</span>
              </td>
              <td :class="r.content_available ? 'text-emerald-600' : 'text-slate-400'">
                {{ r.minor_total }}{{ r.content_available ? '' : '(无)' }}
              </td>
              <td :class="scoreCls(r.metrics?.composite_score)">
                {{ r.metrics?.composite_score ?? '—' }}
              </td>
              <td :class="r.metrics?.limit_hit ? 'text-red-500' : 'text-slate-400'"
                  :title="r.metrics?.limit_hit ? '触顶运行' : ''">
                {{ fmtDuration(r.metrics?.total_latency_ms) ?? '—' }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="!runs.length" class="text-xs text-slate-400">暂无运行数据</p>

      <div id="monitor-runs-pagination" class="monitor-runs-pagination mt-2 flex items-center justify-end gap-2 text-xs">
        <button id="monitor-page-first" class="monitor-page-btn rounded border px-2 py-0.5 hover:bg-slate-50 disabled:opacity-40"
                :disabled="page <= 1" @click="goPage(1)">«</button>
        <button id="monitor-page-prev" class="monitor-page-btn rounded border px-2 py-0.5 hover:bg-slate-50 disabled:opacity-40"
                :disabled="page <= 1" @click="goPage(page - 1)">上一页</button>
        <span id="monitor-page-info" class="monitor-page-info text-slate-500">
          第 {{ page }} / {{ maxPage }} 页 · 每页 {{ PAGE_SIZE }} 条
        </span>
        <button id="monitor-page-next" class="monitor-page-btn rounded border px-2 py-0.5 hover:bg-slate-50 disabled:opacity-40"
                :disabled="page >= maxPage" @click="goPage(page + 1)">下一页</button>
        <button id="monitor-page-last" class="monitor-page-btn rounded border px-2 py-0.5 hover:bg-slate-50 disabled:opacity-40"
                :disabled="page >= maxPage" @click="goPage(maxPage)">»</button>
      </div>
    </section>

    <!-- ④ 选中 run: 时间轴 + 下方详情面板 -->
    <section v-if="selectedRunId" id="monitor-run-detail" ref="detailSection"
             class="monitor-run-detail space-y-3 rounded border bg-slate-50 p-3">
      <header id="monitor-detail-header" class="flex flex-wrap items-center gap-2 rounded border bg-white p-3">
        <div class="min-w-0">
          <h2 id="monitor-detail-title" class="truncate text-base font-semibold text-slate-700">
            时间轴 — {{ selectedRunId }}
          </h2>
          <p class="text-xs text-slate-400">
            {{ selectedRun?.status || '—' }} ·
            {{ selectedRun?.stage_ok ?? 0 }}/{{ selectedRun?.stage_total ?? 0 }} 阶段 ·
            {{ selectedRun?.minor_total ?? 0 }} 内容行
            <span v-if="selectedRun?.parent_run_id"> · 续跑自 {{ selectedRun.parent_run_id }}</span>
          </p>
        </div>
        <button id="monitor-detail-close"
                class="ml-auto rounded border px-2 py-0.5 text-sm text-slate-500 hover:text-slate-700"
                @click="closeRun">✕ 关闭</button>
      </header>

      <div id="monitor-timeline-filters" class="flex flex-wrap items-center gap-2 text-xs">
        <label class="text-slate-500">相位
          <select id="monitor-filter-phase" v-model="phaseFilter"
                  class="ml-1 rounded border bg-white px-1 py-0.5">
            <option value="">全部</option>
            <option value="running">执行中</option>
            <option value="waiting_human">等待人工</option>
            <option value="done">完成</option>
          </select>
        </label>
        <label class="text-slate-500">节点
          <select id="monitor-filter-node" v-model="nodeFilter"
                  class="ml-1 rounded border bg-white px-1 py-0.5">
            <option value="">全部</option>
            <option v-for="n in nodeOptions" :key="n" :value="n">{{ n }}</option>
          </select>
        </label>
        <span class="text-slate-400">显示 {{ timelineNodes.length }} 个节点</span>
        <span v-if="currentStageId" class="ml-auto text-slate-400">当前节点 stage #{{ currentStageId }}</span>
      </div>

      <div v-if="timelineLoading" id="monitor-timeline-loading"
           class="rounded border bg-white p-6 text-center text-sm text-slate-400">
        加载时间轴…
      </div>
      <MonitorTimeline
        v-else
        :timeline="timeline"
        :selected-stage-id="selectedStageId"
        :phase-filter="phaseFilter"
        :node-filter="nodeFilter"
        @select="selectStage"
      />

      <MinorStateDetail
        :detail="stageDetail"
        :loading="detailLoading"
        :config="config"
      />
    </section>
  </div>
</template>
