// SSE 帧解析: 后端每帧是单行 "data: {json}\n\n" (utils.sse_event); 保活为 ": ping" 注释帧

async function checkOk(res) {
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      detail = (await res.json()).detail || detail
    } catch { /* 非 JSON 错误体, 保留状态码 */ }
    throw new Error(detail)
  }
}

// 帧读取与解析共用(连接已建立后):
// - 忽略 ":" 开头的 SSE 注释行(后端保活 ping)
// - 单帧 JSON.parse 失败仅告警跳过, 坏帧不杀整条流
// - 返回 { sawDone }: 是否收到终止帧(done/error), 供调用方判定断流
async function readSSE(res, onEvent) {
  const reader = res.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buf = ''
  let sawDone = false
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let idx
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const frame = buf.slice(0, idx)
      buf = buf.slice(idx + 2)
      const dataLine = frame
        .split('\n')
        .filter((l) => !l.startsWith(':')) // SSE 注释行(": ping" 保活)忽略
        .find((l) => l.startsWith('data:'))
      if (!dataLine) continue
      let e
      try {
        e = JSON.parse(dataLine.slice(5).trim())
      } catch (err) {
        console.warn('SSE 坏帧跳过:', dataLine, err)
        continue
      }
      if (e.event === 'done' || e.event === 'error') sawDone = true
      onEvent(e)
    }
  }
  return { sawDone }
}

// 提问流: /attorney/ask/stream(GET) | /assistant/ask/stream(GET 或 POST)
// body 给定时走 POST(长案情超 URL 长度限制, M11), 不给保持 GET 兼容
export async function streamConsult(url, onEvent, signal, body) {
  const res = await fetch(
    url,
    body
      ? {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
          signal,
        }
      : { signal }
  )
  await checkOk(res)
  const { sawDone } = await readSSE(res, onEvent)
  // 流读尽但未见终止帧: 连接中途断, 不能当完整答案收场(用户主动 abort 会直接抛 AbortError, 不走此分支)
  if (!sawDone) onEvent({ event: 'error', data: '连接中断, 答案可能不完整' })
}

// HITL 恢复流(POST): resume 后的 planner CoT/状态/工具/interrupt/answer 实时下发
export async function streamResume(answer, sessionId, onEvent, signal) {
  const res = await fetch('/api/ask/resume/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, answer }),
    signal,
  })
  await checkOk(res)
  const { sawDone } = await readSSE(res, onEvent)
  if (!sawDone) onEvent({ event: 'error', data: '连接中断, 答案可能不完整' })
}
