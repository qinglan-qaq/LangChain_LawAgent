// 北京时间(UTC+8)格式化 — 后端 TIMESTAMPTZ 存 UTC, 监控页统一展示北京
// ('sv-SE' locale 即 YYYY-MM-DD HH:mm:ss 格式)
export function fmtCST(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  if (isNaN(d.getTime())) return String(iso)
  return d.toLocaleString('sv-SE', { timeZone: 'Asia/Shanghai' })
}

// 短时间(HH:mm:ss), 甘特轴/阶段行用
export function fmtCSTTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (isNaN(d.getTime())) return ''
  return d.toLocaleTimeString('sv-SE', { timeZone: 'Asia/Shanghai' })
}
