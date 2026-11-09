import {
  COLLAPSE_LEN,
  abortCurrentStream,
  resetTurn,
  setSession,
  state,
} from './store'
import { getSessionDetail, getTranscript } from './api'

// 会话 id 前缀决定模式(与后端 AT/AS 命名一致): AS 走律师助理, 其余按代理律师
function modeOf(sid) {
  return sid.startsWith('AS-') ? 'assistant' : 'attorney'
}

// transcript turns → 聊天气泡。恢复只做只读回放, 不重放思考过程/工具事件。
function messagesOf(detail, transcript) {
  const messages = []
  for (const turn of transcript?.turns || []) {
    if (turn.question) {
      messages.push({
        role: 'user',
        text: String(turn.question),
        collapsed: String(turn.question).length > COLLAPSE_LEN,
        done: true,
      })
    }
    for (const supplement of turn.supplements || []) {
      if (!supplement) continue
      messages.push({
        role: 'user',
        kind: 'hitl_answer',
        question: '',
        text: String(supplement),
        done: true,
      })
    }
    if (turn.answer) {
      messages.push({ role: 'assistant', text: String(turn.answer), done: true })
    }
  }
  // checkpoint 不可读时退回会话详情里的首问/终答, 至少能看见会话内容
  if (!messages.length && detail?.query) {
    messages.push({
      role: 'user',
      text: String(detail.query),
      collapsed: String(detail.query).length > COLLAPSE_LEN,
      done: true,
    })
    if (detail.final_answer) {
      messages.push({ role: 'assistant', text: String(detail.final_answer), done: true })
    }
  }
  return messages
}

/**
 * 恢复会话: 切模式、写 sessionId、回放问答、恢复要素面板与待确认中断。
 *
 * 返回会话详情, 供历史抽屉复用, 避免切会话时重复请求 /sessions/{sid}。
 */
export async function restoreSession(sid) {
  const clean = String(sid || '').trim()
  if (!clean) throw new Error('empty_session_id')
  abortCurrentStream()

  // 先定模式: setSession 按当前模式写 localStorage 分键
  state.value.mode = modeOf(clean)
  const [detail, transcript] = await Promise.all([
    getSessionDetail(clean),
    getTranscript(clean).catch(() => null),
  ])
  setSession(clean)
  resetTurn()
  state.value.messages = messagesOf(detail, transcript)
  state.value.elements = detail?.elements || []
  state.value.caseQuery = state.value.elements.length > 0
  state.value.interrupt = detail?.interrupt || null
  return detail
}
