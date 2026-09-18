# 子项目C「Vue 前端产品化 + 双模式咨询」设计 spec

**Goal:** 把现有 LangGraph 法律咨询 Agent 产品化为可用的 Vue 前端应用，提供两种业务模式——
**A. 律师助理模式**（律师粘贴完整案件详情，协助起草婚姻家事类起诉状/答辩状）与
**B. 代理律师模式**（先免责声明，后多轮追问案情，六处 HITL 全程介入）——并在前端实时展示 reasoner 的原生 CoT 思考过程。

**日期:** 2026-09-18 ｜ **状态:** 设计已确认，待实施计划

## 现状证据（2026-09-18 六册体检 + 源码核对）

| 事实 | 位置/证据 |
|---|---|
| 前端仅 59 行骨架（一个 ping 按钮，无聊天/流式/HITL） | `lawApp_LangGraph/law_agent_Vue/src/App.vue` |
| 后端已有完整咨询接口 + SSE 全协议 | `FastAPI/api.py:150-393`（/ask、/ask/resume、/ask/stream 含 token/tool_call/tool_result/interrupt/answer/session_id/done/error 事件） |
| 六处 `with_structured_output` 默认 json_schema 被 DeepSeek 拒（HTTP 400），三处 HITL（①②⑤）死 | `LangGraph_lawApp.py:284/358/532/911/1001/1077`；02 册实测；`json_mode` 两模型实测可用 |
| SSE 无 reasoning 事件 | api.py grep "reasoning" 为空 |
| PG 认证失败（DB_PASSWORD 默认值）→ 检索/记忆/checkpointer 降级 InMemory | 03/04 册实测 |
| 入库脚本缺失，law_cases 空表 | `RAG_service/pgvector_retriever.py:6` 引用不存在的 `scripts/ingest_cases_pgvector.py` |
| 既有「角色」钩子仅切换分析工具人设（kim/saul），非模式系统 | `prompts.py:310` |
| AgentState 无 mode 字段；`element_assess` 本就只判婚姻家事类（与模式 A 业务天然同域） | `state.py`；`LangGraph_lawApp.py:358` 分支 |

## 技术栈

**前端:** Vue 3.5 + Vite 8 + **Tailwind CSS v4** + **Inspira UI**（unovue/inspira-ui，MIT，shadcn-vue 式复制安装）+ motion-v + lucide-vue-next + reka-ui。不引 router/Pinia（轻量 reactive store 模块）。
**后端:** 现有 FastAPI/LangGraph 1.0.1/DeepSeek 双模型（planner=reasoner, executor=chat），图结构零改动。

## 架构与依赖链

```
Phase 0 地基修复 ──→ Phase 1 后端(双模式接口 + CoT SSE + sessions) ──→ Phase 2 前端主体
                          │ planner/replanner 手工流式改造(§1.3)          ↑ 可提前搭壳(静态 mock)
                          └────────── 与 Phase 1 并行 ──────────────────┘
```

---

## Phase 0 — 地基修复（前端动工前置，不做则产品是空壳）

**P0.1 结构化输出改 json_mode：** 六处中四处（`LangGraph_lawApp.py:284` RiskSchema、`:358` ElementAssessmentSchema、`:911` ReplanCheckSchema、`:1001` MidClarifySchema）改为 `method="json_mode"` 且对应提示词显式声明字段名；返回值经 Pydantic 手动校验兜底（json_mode 不校验 schema）。复活 HITL①②⑤ 与要素反问。
**P0.2 planner/replanner（`:532`/`:1077`）不在此改**——与 Phase 1 的 CoT 手工流式改造合并为一处（见 P1.2），避免改两遍。
**P0.3 DB_PASSWORD 填真实值**（配置项，非代码）——会话历史界面依赖 PG checkpointer。
**P0.4 新建 `scripts/ingest_cases_pgvector.py` + `embedder.embed_documents` 批量接口**——data/ 语料入库 law_cases，答复才有引用来源。
**验证:** 重跑 tests_ipynb 02/03/04/06 册，06 的「计划环节未走兜底」「答复有检索依据」两条转 PASS。

## Phase 1 — 后端双模式 + CoT + sessions

### P1.1 双模式拆为两个独立 FastAPI 接口（用户决策）

```
代理律师模式(B):
  POST /attorney/ask          完整咨询(阻塞式, QueryResponse)
  GET  /attorney/ask/stream   SSE 流式咨询(token/tool_call/tool_result/reasoning/interrupt/answer/session_id/done/error)
  免责硬校验: AttorneyAskRequest.disclaimer_ack 非 true → 403 + {disclaimer: 声明文本};
              true → 放行 + 写 audit(事件类型 disclaimer_ack)

律师助理模式(A):
  POST /assistant/ask         文书起草(阻塞式)
  GET  /assistant/ask/stream   SSE 流式起草(CoT + 法条/案例检索过程)
  请求体: AssistantAskRequest{case_details: str, doc_type: "complaint"|"defense", session_id: str = ""}

共用(session 维度):
  POST /ask/resume            六类 HITL 恢复(两模式共用)
  GET /sessions               会话列表(时间/标题/轮次, 从 checkpointer 读)
  GET /sessions/{sid}         会话详情(各轮答复/interrupt 记录/要素终态)
  保留不动: /tools /home /feedback /ask/pdf
  旧 /ask、/ask/stream 保留一期标 deprecated(存量测试与体检册 06 依赖), 二期移除
```

- `QueryRequest` 拆为 `AttorneyAskRequest`（query + disclaimer_ack + session_id）与 `AssistantAskRequest`；**mode 由端点决定，不作为请求字段**。
- `AgentState` 增 `mode: str = "attorney"` 字段，`ingest` 节点按端点注入；planner/finalize 按 mode 选提示词变体（P1.3）。图结构、路由函数、六处 interrupt 零改动。
- 模式 A 文书要素提取复用 `case_elements` 机制但换文书要素集（原被告信息/诉讼请求/事实与理由/证据清单）；要素缺失同样走 `ask_element` 反问 HITL——两模式共用 `/ask/resume` 的依据。

### P1.2 真·reasoner CoT 流（planner/replanner 手工流式）

`:532` planner 与 `:1077` replanner 弃用 `with_structured_output`，改手工流式调用：
- 逐 delta 转发 `reasoning_content` 为新 SSE 事件 `{"event": "reasoning", "source": "planner"|"replanner", "delta": "..."}`；
- 同时累积 `content`，流结束后手动 JSON 解析 + PlanSchema Pydantic 校验；
- 解析/校验失败 → 落回既有硬编码兜底计划（保底行为与现状一致，06 册断言的降级检查不受影响——正常路径无 fallback 字样）。
- 其余四处节点（risk/assess/replan_check/mid_clarify）保持 P0.1 的 json_mode 非流式。

### P1.3 提示词体系（prompts.py 新增）

- `PLANNER_PROMPT_ATTORNEY`（多轮追问案情导向）/ `PLANNER_PROMPT_ASSISTANT`（要素提取→检索→文书结构化起草导向）。
- `FINALIZE_COMPLAINT_PROMPT` / `FINALIZE_DEFENSE_PROMPT`：婚姻家事类（离婚纠纷/抚养权/财产分割）起诉状/答辩状模板，固定段落结构（当事人→诉讼请求→事实与理由→证据清单），文末固定「AI 生成，需律师复核」标注。
- `DISCLAIMER_TEXT` 常量：代理律师模式免责声明（AI 非执业律师、不构成法律意见、紧急情况热线），供 403 响应与前端弹窗共用同一来源。

### P1.4 SSE 传输

第一期沿用 GET + query 传参；前端用 fetch + ReadableStream 读取（可主动 abort、不自动重连——规避 EventSource 自动重连导致整图重复执行烧配额）。`POST /attorney|assistant/ask/stream` 为二期项。长文本防护：query 超长（>4000 字符）返回 414 语义的明确错误。

## Phase 2 — 前端主体（仓库根 `frontend/`，自 `lawApp_LangGraph/law_agent_Vue/` 迁出）

```
frontend/
├── src/
│   ├── App.vue              # 布局: 模式切换 + 左历史栏 + 右工作区
│   ├── store.js             # reactive: messages/tools/elements/interrupt/session/mode
│   ├── sse.js               # fetch 流式读 + 事件分发(token/reasoning/tool_*/interrupt/answer/done/error)
│   ├── api.js               # /attorney/ask /assistant/ask /ask/resume /sessions
│   └── components/
│       ├── ModeSwitch.vue       # 律师助理 / 代理律师
│       ├── DisclaimerModal.vue  # B 模式进入免责声明(确认→disclaimer_ack=true)
│       ├── ChatView.vue         # 消息流 + token 渐进渲染
│       ├── ThinkingPanel.vue    # CoT 折叠面板, reasoning 逐字流入
│       ├── ToolTimeline.vue     # 工具调用时间线
│       ├── ElementPanel.vue     # 案件/文书要素面板
│       ├── CitationList.vue     # 引用来源(法条+案例)
│       ├── InterruptPanel.vue   # 六类分型: 是/否钮(risk_confirm/pdf_confirm)·文本输入(clarify/mid_clarify)·选项钮(degrade_confirm/budget_confirm) → 统一 POST /ask/resume
│       ├── DocDraft.vue         # A 模式: 案情大文本输入 + doc_type 选择 + 文书预览/复制
│       └── HistorySidebar.vue   # 会话列表 + 回看
```

**Inspira UI 组件映射（复制安装，缺则 Tailwind v4 手写补）：**

| 页面块 | 组件 |
|---|---|
| 消息流 | Chat Group / Chat Bubble |
| CoT 面板 | Fade-In + Typing Animation |
| 工具时间线 | Timeline |
| 要素面板/引用 | Bento Grid / Magic Card |
| 活动 interrupt 高亮 | Border Beam |
| 免责弹窗 | reka-ui Dialog 基座 |
| 背景/发送按钮 | Dot Pattern / Shimmer Button |

**交互闭环:** B 模式: 免责弹窗确认 → 提问 → SSE（reasoning 思考流 + 工具时间线 + 要素面板实时更新）→ interrupt 弹面板 → resume → 续答 → done；session_id 存 localStorage 续聊。A 模式: 粘贴案情 + 选文书类型 → SSE（CoT + 检索过程）→ 文书预览/复制 →（要素缺失时同样 interrupt 反问）。

**工程:** `lawApp_LangGraph/law_agent_Vue/` 整体迁至仓库根 `frontend/`（当前嵌在 Python 包内，52MB 含 node_modules/dist，打包 Python 时会被带走）；vite proxy `/api → 127.0.0.1:8000` 不变。

## 错误处理

- 后端所有新端点异常路径返回结构化错误（复用 `build_response` 风格），SSE 以 `error` 事件收尾，不裸抛堆栈。
- CoT 流中断/JSON 解析失败 → 兜底计划 + SSE `reasoning_done` 事件标注降级，前端思考面板标灰提示。
- PG 不可用时 sessions 端点返回明确降级文案（InMemory 下历史为空），不 500。
- 前端 fetch 流 abort 后显式断开提示，Session 状态不残留「进行中」。

## 测试与验证

- **后端:** pytest 新增（复用 `tests/test_smoke.py` 的 no_llm/_FakeLLM 替身，不联网不调真 LLM）：双端点路由、403 免责校验、audit 留痕、SSE reasoning 事件存在性、PlanSchema 手动解析成功/失败两分支、sessions 端点（InMemory 降级分支）。
- **前端:** `npm run build` 零错；人工走两模式全流程（含至少一次 interrupt 与 resume）。
- **回归:** tests_ipynb 06 册端到端（旧 /ask 路径）不回归；02 册 json_mode 断言全绿。

## 明确不做（第一期）

合同审查/律师函/证据清单类文书、登录鉴权、POST 流式端点、UI 主题深加工、前后端部署合并（分跑 + vite proxy）、非婚姻家事类模板、多用户并发治理。

## 风险与对策

| 风险 | 对策 |
|---|---|
| reasoner 流式下 content 偶发不完整/非法 JSON | 手动解析 + Pydantic 校验，失败落既有兜底计划（P1.2），行为保底 |
| Inspira UI 组件与业务场景不完全匹配 | 复制安装哲学即允许改造；缺失组件 Tailwind v4 手写补 |
| 前端提前搭壳与后端接口演进不同步 | sse.js/api.js 单独成层, 后端字段变更只动这两处 |
| GET + query 长案情文本超 URL 限制 | 414 语义明确报错 + A 模式引导分段粘贴; POST 流式二期根治 |
| 六册体检与前端产品并行演进导致回归盲区 | run_all.py + pytest 全绿作为每阶段完成门槛 |
