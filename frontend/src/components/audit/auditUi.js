// 审计视图公共展示工具: 等级配色/耗时/状态文案。
export const LEVEL_UI = {
  good: { label: '通过', cls: 'border-emerald-200 bg-emerald-50 text-emerald-700' },
  warn: { label: '注意', cls: 'border-amber-200 bg-amber-50 text-amber-700' },
  bad: { label: '异常', cls: 'border-red-200 bg-red-50 text-red-700' },
  unknown: { label: '未知', cls: 'border-slate-200 bg-slate-50 text-slate-600' },
  na: { label: '不适用', cls: 'border-slate-200 bg-white text-slate-400' },
}

export const STATUS_CLS = {
  ok: 'text-emerald-600',
  error: 'text-red-500',
  interrupted: 'text-amber-600',
  running: 'text-blue-600',
  waiting_human: 'text-amber-600',
}

export const KIND_LABEL = {
  law: '法条',
  case: '案例',
  memory: '记忆',
  web: '网络',
}

export function fmtMs(n) {
  if (n == null || Number.isNaN(Number(n))) return '—'
  const v = Number(n)
  return v < 1000 ? `${v}ms` : v < 60000 ? `${(v / 1000).toFixed(1)}s` : `${(v / 60000).toFixed(1)}m`
}

export function fmtNum(n) {
  return n == null ? '—' : Number(n).toLocaleString('zh-CN')
}

export function levelOf(level) {
  return LEVEL_UI[level] || LEVEL_UI.unknown
}

export function shortId(id) {
  return id ? String(id).slice(0, 18) : '—'
}
