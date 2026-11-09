// 自动录制演示视频：真实浏览器 + 真实后端，不走 mock。
//
// 用法（在仓库根目录）：
//   npm i -D playwright && npx playwright install chromium
//   node docs/demo/record_demo.mjs
//
// 可选环境变量：
//   BASE_URL=http://127.0.0.1:5173
//   OUT_DIR=docs/demo/output
//   HEADLESS=0            显示浏览器窗口（默认无头录制）
//   PLAYWRIGHT_MODULE=... 指定 playwright 模块（ESM import 说明符）
//
// 产出：
//   docs/demo/output/demo-raw-<ts>.webm   原始录像
//   docs/demo/output/recording-log-<ts>.json 分段/状态时间戳（配音对齐用）

import { mkdir, writeFile, copyFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const repoRoot = path.resolve(__dirname, '../..')
const BASE_URL = process.env.BASE_URL || 'http://127.0.0.1:5173'
const OUT_DIR = path.resolve(repoRoot, process.env.OUT_DIR || 'docs/demo/output')
const HEADLESS = process.env.HEADLESS !== '0'

const pw = await import(process.env.PLAYWRIGHT_MODULE || 'playwright')
const { chromium } = pw

// 高风险拦截演示：必然触发 risk_confirm（家暴关键词）
const RISK_QUERY =
  '我丈夫长期家暴，我想离婚，孩子3岁，房子是婚后买的，我能争取抚养权和房子吗？'
const RISK_CLARIFY_ANSWER =
  '他不同意离婚，也不同意孩子归我。房子是婚后共同购买、登记在双方名下，贷款一起还。我有报警记录和伤情照片。'

// 完整链路演示：要素一次给全，避免中途追问，直接走 CRAG 计划与执行
const FULL_QUERY =
  '我和丈夫2018年5月登记结婚，2020年3月生了一个儿子。他长期家暴，2025年6月我报警并搬走，现在分居。' +
  '我想离婚，孩子由我抚养，他每月付3000元抚养费，婚后买的朝阳区房子登记在双方名下、贷款共同还，要求依法分割。' +
  '我有报警记录、告诫书、伤情照片、医院诊断、房产证、贷款合同、孩子出生证明和微信聊天记录。' +
  '他现在不同意离婚，也不同意孩子归我，说房子要留着自己住。'

const FALLBACK_ANSWERS = [
  '他不同意离婚，也不同意孩子归我。房子登记在双方名下，贷款共同偿还。',
  '我有报警记录、告诫书、伤情照片、医院诊断和微信聊天记录。',
  '我们2018年结婚，2025年6月分居至今，孩子一直由我主要照顾。',
]

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const t0 = Date.now()
const marks = []
const statusLog = []

function mark(name, extra = {}) {
  const item = { name, tMs: Date.now() - t0, ...extra }
  marks.push(item)
  console.log(`[${(item.tMs / 1000).toFixed(1).padStart(6)}s] ${name}`, extra.detail || '')
}

function logStatus(text) {
  const now = Date.now() - t0
  const last = statusLog[statusLog.length - 1]
  if (!last || last.text !== text) {
    statusLog.push({ tMs: now, text })
  }
}

async function scrollBottom(page) {
  await page.evaluate(() => {
    const main = document.querySelector('main')
    if (main) main.scrollTop = main.scrollHeight
  })
}

async function typeQuery(page, text) {
  const input = page.locator('#input')
  await input.click()
  await input.pressSequentially(text, { delay: 12 })
  await sleep(800)
  await input.press('Enter')
}

async function waitGone(page, selector, timeout = 15000) {
  await page.waitForFunction(
    (sel) => !document.querySelector(sel),
    selector,
    { timeout }
  ).catch(() => {})
}

async function waitInterrupt(page, timeout = 120000) {
  await page.waitForSelector('#choose', { timeout })
  await sleep(1500)
  return (await page.locator('#choose').innerText()).replace(/\s+/g, ' ').trim()
}

async function answerInterrupt(page, text, label = 'interrupt') {
  const panelText = (await page.locator('#choose').innerText()).replace(/\s+/g, ' ').trim()
  console.log(`    panel: ${panelText.slice(0, 120)}`)

  // 确认型：点对应选项，再点「确认」
  if (panelText.includes('高风险确认')) {
    await page.locator('#choose-opt-确认').click().catch(() => {})
    await page.getByRole('button', { name: '确认', exact: true }).click()
    mark(`${label}:risk-confirmed`)
    return panelText
  }

  const textInput = page.locator('#choose input[placeholder*="直接输入"]')
  const radios = page.locator('#choose input[type="radio"]')

  if (await textInput.count()) {
    await textInput.fill(text)
    await sleep(400)
    await textInput.press('Enter')
    mark(`${label}:text-answered`, { detail: text.slice(0, 40) })
  } else if (await radios.count()) {
    await radios.first().check()
    const submit = page.getByRole('button', { name: '提交', exact: true })
    if (await submit.count()) await submit.click()
    else await page.getByRole('button', { name: '确认', exact: true }).click()
    mark(`${label}:option-answered`)
  } else {
    await page.getByRole('button', { name: '确认', exact: true }).click()
    mark(`${label}:confirmed`)
  }
  return panelText
}

// 等待「出现新的 interrupt」或「出现已完成终答」，谁先到算谁
async function waitInterruptOrResult(page, timeout = 360000) {
  const start = Date.now()
  while (Date.now() - start < timeout) {
    if (await page.locator('#choose').count()) return 'interrupt'
    if (await page.locator('[id^="result_llm-"]').count()) return 'result'
    const err = page.locator('p.text-red-600')
    if (await err.count()) return 'error'
    await sleep(1500)
  }
  return 'timeout'
}

async function main() {
  await mkdir(OUT_DIR, { recursive: true })
  const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)

  const browser = await chromium.launch({ headless: HEADLESS, args: ['--lang=zh-CN'] })
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    locale: 'zh-CN',
    recordVideo: { dir: path.join(OUT_DIR, '.tmp'), size: { width: 1440, height: 900 } },
  })
  const page = await context.newPage()
  page.setDefaultTimeout(30000)

  // ---------- 1. 开场：落地页 / 双模式 / 免责声明 ----------
  mark('intro:start')
  await page.goto(BASE_URL, { waitUntil: 'networkidle' })
  await sleep(3000)
  await page.screenshot({ path: path.join(OUT_DIR, `shot-${stamp}-01-landing.png`) })
  await page.locator('#disclaimer-toast button').click().catch(() => {})
  await sleep(1500)
  await page.locator('#mode-switch').hover()
  await sleep(1500)
  mark('intro:end')

  // ---------- 2. 高风险拦截 + 要素追问（HITL） ----------
  await page.locator('#btn-new-session').click()
  await sleep(800)
  mark('hitl:start')
  await typeQuery(page, RISK_QUERY)
  mark('hitl:submitted')
  await waitInterrupt(page, 120000)
  await page.screenshot({ path: path.join(OUT_DIR, `shot-${stamp}-02-risk.png`) })
  mark('hitl:risk-card')
  await answerInterrupt(page, RISK_CLARIFY_ANSWER, 'hitl')
  await waitGone(page, '#choose', 20000)
  await waitInterrupt(page, 120000)
  await page.screenshot({ path: path.join(OUT_DIR, `shot-${stamp}-03-clarify.png`) })
  mark('hitl:clarify-card')
  await answerInterrupt(page, RISK_CLARIFY_ANSWER, 'hitl')
  await sleep(9000)
  await scrollBottom(page)
  await sleep(2000)
  await page.screenshot({ path: path.join(OUT_DIR, `shot-${stamp}-04-hitl-answer.png`) })
  mark('hitl:end')

  // 切模式会 abort 在途流，回到干净的新会话
  await page.getByRole('button', { name: '律师助理', exact: true }).click()
  await sleep(600)
  await page.getByRole('button', { name: '代理律师', exact: true }).click()
  await sleep(1000)
  await page.locator('#btn-new-session').click()
  await sleep(800)

  // ---------- 3. 完整链路：计划 → 工具 → 质量门控 → 终答 ----------
  mark('flow:start')
  await typeQuery(page, FULL_QUERY)
  mark('flow:submitted')

  let autoScroll = true
  const scrollTimer = setInterval(async () => {
    if (!autoScroll) return
    try {
      await page.evaluate(() => {
        const main = document.querySelector('main')
        if (main) main.scrollTop = main.scrollHeight
      })
      const status = await page.locator('#agent-status-bar').innerText().catch(() => '')
      if (status) logStatus(status.replace(/\s+/g, ' ').trim())
    } catch {
      /* page may be closing */
    }
  }, 2500)

  let fallbackIdx = 0
  let outcome = 'interrupt'
  while (outcome === 'interrupt') {
    outcome = await waitInterruptOrResult(page, 360000)
    if (outcome === 'interrupt') {
      await sleep(1200)
      await page.screenshot({
        path: path.join(OUT_DIR, `shot-${stamp}-05-flow-interrupt-${fallbackIdx}.png`),
      })
      await answerInterrupt(page, FALLBACK_ANSWERS[fallbackIdx % FALLBACK_ANSWERS.length], 'flow')
      fallbackIdx += 1
      await waitGone(page, '#choose', 20000)
    }
  }
  autoScroll = false
  clearInterval(scrollTimer)
  mark('flow:done', { detail: outcome })

  if (outcome !== 'result') {
    await page.screenshot({ path: path.join(OUT_DIR, `shot-${stamp}-99-failure.png`) })
    console.error(`演示链路未产出终答：${outcome}`)
  } else {
    // 展开终答与思考过程，给后期留出可看的画面
    await page.locator('[id^="result_llm-"]').last().scrollIntoViewIfNeeded()
    await sleep(1500)
    const expand = page.locator('#chat button:has-text("展开全文")')
    if (await expand.count()) await expand.last().click().catch(() => {})
    await sleep(2000)
    await page.screenshot({ path: path.join(OUT_DIR, `shot-${stamp}-06-answer.png`) })
    mark('flow:answer-shown')

    const thinking = page.locator('#chat summary:has-text("思考过程")')
    if (await thinking.count()) {
      await thinking.last().click().catch(() => {})
      await sleep(2500)
      await page.screenshot({ path: path.join(OUT_DIR, `shot-${stamp}-07-thinking.png`) })
      mark('flow:thinking-open')
    }

    // 慢慢向下滚，展示答案结构、引用与免责声明
    for (let i = 0; i < 4; i++) {
      await page.mouse.wheel(0, 520)
      await sleep(1400)
    }
    await scrollBottom(page)
    await sleep(2500)
    await page.screenshot({ path: path.join(OUT_DIR, `shot-${stamp}-08-answer-bottom.png`) })
    mark('flow:end')
  }

  // ---------- 收尾 ----------
  await page.screenshot({ path: path.join(OUT_DIR, `shot-${stamp}-09-final.png`) }).catch(() => {})
  const video = page.video()
  await context.close()
  await browser.close()

  const rawPath = await video.path()
  const finalVideo = path.join(OUT_DIR, `demo-raw-${stamp}.webm`)
  await copyFile(rawPath, finalVideo)

  const logFile = path.join(OUT_DIR, `recording-log-${stamp}.json`)
  await writeFile(
    logFile,
    JSON.stringify(
      { baseUrl: BASE_URL, startedAt: new Date(t0).toISOString(), durationMs: Date.now() - t0, marks, statusLog, video: finalVideo },
      null,
      2
    ),
    'utf8'
  )

  console.log('\nVIDEO', finalVideo)
  console.log('LOG  ', logFile)
}

await main()
