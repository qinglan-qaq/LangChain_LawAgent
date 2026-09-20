// SSE 帧解析: 后端每帧是单行 "data: {json}\n\n" (utils.sse_event)

async function checkOk(res) {
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      detail = (await res.json()).detail || detail
    } catch { /* 非 JSON 错误体, 保留状态码 */ }
    throw new Error(detail)
  }
}

// 帧读取与解析共用(连接已建立后)
async function readSSE(res, onEvent) {
  const reader = res.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buf = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let idx
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const frame = buf.slice(0, idx)
      buf = buf.slice(idx + 2)
      const dataLine = frame.split('\n').find((l) => l.startsWith('data:'))
      if (dataLine) onEvent(JSON.parse(dataLine.slice(5).trim()))
    }
  }
}

// 提问流(GET): /attorney/ask/stream | /assistant/ask/stream
export async function streamConsult(url, onEvent, signal) {
  const res = await fetch(url, { signal })
  await checkOk(res)
  return readSSE(res, onEvent)
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
  return readSSE(res, onEvent)
}
