// SSE 帧解析: 后端每帧是单行 "data: {json}\n\n" (utils.sse_event)
export async function streamConsult(url, onEvent, signal) {
  const res = await fetch(url, { signal })
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      detail = (await res.json()).detail || detail
    } catch { /* 非 JSON 错误体, 保留状态码 */ }
    throw new Error(detail)
  }
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
