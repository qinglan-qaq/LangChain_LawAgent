<!-- MonitorView.vue
  监控页(D-spec §六): 总览卡 → runs 表 → 甘特详情 → 阶段抽屉;
  3s 轮询, 无 running 行停轮。
-->
<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import {
  getMonitorEvals, getMonitorOverview, getMonitorRunDetail, getMonitorRuns,
} from '../api'
import GanttTimeline from '../components/GanttTimeline.vue'
import StageDetailDrawer from '../components/StageDetailDrawer.vue'
import { fmtCST } from '../lib/time'

const overview = ref({})
const runs = ref([])
const evals = ref([])
const detail = ref(null)          // MonitorRunDetail
const selectedStage = ref(null)   // MonitorStage
const timer = ref(null)
const now = ref(Date.now())

async function refresh() {
  const [ov, rs, ev] = await Promise.all([
    getMonitorOverview(), getMonitorRuns(50), getMonitorEvals(),
  ])
  overview.value = ov
  runs.value = rs
  evals.value = ev
  const anyRunning = rs.some((r) => r.stage_running > 0)
  now.value = Date.now()
  if (anyRunning && !timer.value) {
    timer.value = setInterval(tick, 3000)
  } else if (!anyRunning && timer.value) {
    clearInterval(timer.value)
    timer.value = null
  }
}

async function tick() {
  runs.value = await getMonitorRuns(50)
  overview.value = await getMonitorOverview()
  if (detail.value) {
    detail.value = await getMonitorRunDetail(detail.value.run_id)
  }
  now.value = Date.now()
}

async function openRun(r) {
  detail.value = await getMonitorRunDetail(r.run_id)
  selectedStage.value = null
}

function scoreCls(s) {
  if (s == null) return 'text-slate-400'
  return s >= 80 ? 'text-emerald-600' : s >= 60 ? 'text-amber-600' : 'text-red-500'
}

onMounted(refresh)
onUnmounted(() => timer.value && clearInterval(timer.value))
</script>

<template>
  <div class="h-screen overflow-y-auto bg-slate-100 p-4 space-y-4">
    <header class="flex items-center gap-3 border-b pb-2 bg-white/80 p-3 rounded">
      <router-link to="/" class="text-sm text-blue-600 hover:underline">← 返回咨询</router-link>
      <h1 class="text-lg font-semibold text-slate-700">Agent 阶段监控</h1>
      <span v-if="overview.running_stages" class="text-xs text-blue-600">
        {{ overview.running_stages }} 个阶段进行中
      </span>
    </header>

    <!-- ① 总览卡 -->
    <section class="grid grid-cols-2 md:grid-cols-4 gap-3">
      <div class="border rounded bg-white p-3 text-sm">
        <p class="text-slate-400 text-xs">近24h 运行</p>
        <p class="text-xl font-semibold text-slate-700">
          {{ Object.values(overview.runs_by_status || {}).reduce((a, b) => a + b, 0) }}
        </p>
        <p class="text-xs text-slate-500">{{ overview.runs_by_status }}</p>
      </div>
      <div class="border rounded bg-white p-3 text-sm">
        <p class="text-slate-400 text-xs">进行中阶段</p>
        <p class="text-xl font-semibold text-blue-600">{{ overview.running_stages ?? 0 }}</p>
      </div>
      <div class="border rounded bg-white p-3 text-sm">
        <p class="text-slate-400 text-xs">触顶运行(limit_hit)</p>
        <p class="text-xl font-semibold" :class="overview.limit_hit_runs ? 'text-red-500' : 'text-slate-700'">
          {{ overview.limit_hit_runs ?? 0 }}
        </p>
      </div>
      <div class="border rounded bg-white p-3 text-sm">
        <p class="text-slate-400 text-xs">节点失败排行(24h)</p>
        <p v-for="f in overview.node_fail_top || []" :key="f.node_name"
           class="text-xs text-slate-600">{{ f.node_name }}: {{ f.errors }}</p>
        <p v-if="!(overview.node_fail_top || []).length" class="text-xs text-slate-400">无</p>
      </div>
    </section>

    <!-- ② 指标看板卡(评测批次) -->
    <section class="border rounded bg-white p-3">
      <h2 class="text-sm font-semibold text-slate-600 mb-2">评测批次</h2>
      <table class="w-full text-xs" v-if="evals.length">
        <thead class="text-slate-400 text-left">
          <tr><th class="py-1">时间</th><th>数据集</th><th>标签</th>
              <th>hit_rate@5</th><th>MRR@5</th><th>P@5</th><th>R@5</th><th>F1@5</th></tr>
        </thead>
        <tbody class="text-slate-600">
          <tr v-for="e in evals" :key="e.id" class="border-t">
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

    <!-- ③ runs 列表 -->
    <section class="border rounded bg-white p-3">
      <h2 class="text-sm font-semibold text-slate-600 mb-2">运行列表(50)</h2>
      <table class="w-full text-xs" v-if="runs.length">
        <thead class="text-slate-400 text-left">
          <tr><th class="py-1">开始时间</th><th>run_id</th><th>会话</th><th>模式</th>
              <th>状态</th><th>阶段(ok/总)</th><th>综合分</th><th>触顶</th></tr>
        </thead>
        <tbody>
          <tr v-for="r in runs" :key="r.run_id"
              class="border-t cursor-pointer hover:bg-slate-50"
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
    </section>

    <!-- ④ 甘特 + 抽屉 -->
    <section v-if="detail" class="grid md:grid-cols-3 gap-3">
      <div class="md:col-span-2">
        <h2 class="text-sm font-semibold text-slate-600 mb-2">
          甘特时间线 — {{ detail.run_id }}
        </h2>
        <GanttTimeline :stages="detail.stages" :now="now"
                      @select="selectedStage = $event" />
      </div>
      <StageDetailDrawer :stage="selectedStage" :spans="detail.spans" />
    </section>
  </div>
</template>
