# 统一审计监控页（Unified Audit View）— 设计文档

- 日期: 2026-11-08
- 状态: 已实施（2026-11-09 验收）
- 范围: 监控页信息架构、审计视图模型、采集补齐、API 契约、 phased 实施计划
- 前置: [2026-10-08-stage-chain-monitoring-design.md](2026-10-08-stage-chain-monitoring-design.md)、[2026-10-23-minor-states-monitoring-design.md](2026-10-23-minor-states-monitoring-design.md)

## Problem Statement

现有监控页已经能看 run 列表、垂直时间轴、节点详情和原始 JSON，但审计时仍然低效：

1. **调用与证据断裂**：工具调用、检索文档、评估结论、终答引用之间没有稳定关联，`refs_resolved` 多数为空。
2. **JSON 过多且无主次**：`output_json`、`context_meta`、`span.state` 直接平铺，页面首屏被原始结构占满，抓不住重点。
3. **审计流程不清晰**：没有 run 级结论，也没有固定审计顺序；证据、上下文、性能分散在不同折叠块里。
4. **循环噪音大**：`executor/tools/merge` 反复出现，27 个 stage 平铺后主路径难辨。
5. **续跑链不连贯**：HITL 暂停与 resume 产生多个 run，页面按单 run 展示，跨 run 关系不明显。

目标不是增加更多原始数据，而是增加**审计视图层**：从现有事实表派生一份统一、分层、可下钻的审计模型，页面默认只显示结论与摘要，全文按需加载。

## 已确认决策（brainstorming 访谈）

| # | 问题 | 决策 |
|---|------|------|
| D1 | 首要用途 | **审计复盘为主，演示讲解为辅** |
| D2 | 下钻粒度 | **全链路逐层**：节点 → 调用 → 证据 → 上下文 → 引用 |
| D3 | 监控对象 | **单次提问全链**，含 planner 循环与 HITL resume 产生的多个 run |
| D4 | 当前痛点 | 调用与证据断裂；JSON 过多无重点；审计流程不清晰 |
| D5 | 留存策略 | **摘要常驻 + 全文按需**，不复制完整 prompt/上下文 |
| D6 | 审计必答问题 | 证据链是否成立、上下文是否正确、性能与异常在哪 |
| D7 | 页面方向 | **A+C 混合**：审计报告单页 + 保留执行时间轴节点 |
| D8 | 统一口径 | 一个 `AuditView` 模型、一个后端 builder、一套 API，时间轴/证据/上下文/性能/抽屉全部消费同一模型 |

## Goals

- 打开一次提问的审计页，10 秒内回答三个问题：证据链是否成立、上下文是否正确、性能与异常在哪。
- 保留现有垂直时间轴和节点卡，循环折叠为“计划步骤组”，主路径可见。
- 每个终答引用可回到证据，每份证据可回到工具调用/span，每次工具调用可回到节点。
- 页面首屏不出现大段原始 JSON；prompt、工具原始参数、`AgentState` 只在“原始数据”tab 按需加载。
- 新旧 run 都能看；旧 run 缺采集字段时明确标 `partial/unknown`，不伪造完整。
- 不改动 Agent 图语义、不新增数据库表、不破坏现有 `/monitor/*` 端点。

## Non-Goals

- 不做 DAG 流程图/双视图（方案 B）。
- 不做 LLM-as-judge 自动审计评分。
- 不做跨会话多轮对比的独立产品视图。
- 不做权限系统或远程多用户隔离；本期仍是本地单机演示/审计。
- 不复制完整 prompt/context 到新表；原文继续从 `trace_spans` 读取。

## 统一审计模型

### 设计原则

1. **事实层与视图层分离**：`trace_runs/stage_chain/minor_states/trace_spans` 是事实；`AuditView` 是确定性派生，不参与 Agent 决策。
2. **一个 builder**：`lawApp_LangGraph/audit_view.py` 同时服务审计页、增量更新和导出，禁止前端各自解析原始 JSON。
3. **摘要默认、全文按需**：`AuditView` 不包含 prompt、工具原始参数、完整 state；这些只出现在 `audit-detail?include_raw=1`。
4. **可追溯**：模型内所有证据、引用、上下文都带稳定 `stage_id/span_id/evidence_id`。
5. **缺失显式化**：缺 `minor_states`、缺 span、旧 run 缺 evidence id，都以 `unknown/partial` 和 anomaly 表达。

### 顶层结构

```text
AuditView {
  schema_version: 1
  generated_at: ISO8601
  degraded: bool
  run: RunHeader
  chain: ChainRun[]
  verdicts: AuditVerdicts
  timeline: TimelineGroup[]
  evidence: EvidenceBoard
  context: ContextMatrix
  performance: PerformanceBoard
  anomalies: Anomaly[]
}
```

### RunHeader

```text
RunHeader {
  run_id, session_id, parent_run_id, root_run_id,
  run_type, mode, query, status,
  started_at, ended_at, latency_ms,
  current_node, stage_count, minor_count, answer_len
}
```

### ChainRun

```text
ChainRun {
  run_id, parent_run_id, status,
  started_at, ended_at, latency_ms, wait_before_ms,
  stage_count, has_audit_content
}
```

`chain` 沿 `parent_run_id` 最多回溯 3 层，时间升序；`wait_before_ms` 是上一个 run 结束到本 run 开始的空档，对应人工等待。

### AuditVerdicts

四张结论卡，每张都有 `level` 和可跳转目标：

```text
Verdict {
  key: flow | evidence | context | performance
  level: good | warn | bad | unknown | na
  summary: string
  reasons: string[]
  jump: {section: string, stage_id?: int, evidence_id?: string}
}

level 规则:
- flow: run 成功且无节点错误 → good（HITL 本身不扣分）；
        触发 replan 或有重试 → warn；失败/中断 → bad
- evidence: 证据充足且引用全部可解析 → good；
            证据不足或存在未解析引用 → warn；
            有检索但无任何可用证据 → bad；
            闲聊/无需检索 → na；旧 run 缺关联字段 → unknown
- context: 无截断且关键段齐全 → good；
           有截断或部分节点缺 meta → warn；
           关键段缺失 → bad；旧 run 无 context_meta → unknown
- performance: 无错误且慢点低于阈值 → good；
               单节点超阈值或重试 → warn；错误/触顶 → bad
```

### TimelineGroup / AuditNode

时间轴不再平铺所有 stage，按“计划步骤”分组：

```text
TimelineGroup {
  group_id, kind: decision | step | hitl | answer,
  plan_round: int, step_index: int | null,
  label: string,
  stage_ids: int[],
  node_names: string[],
  started_at, ended_at, latency_ms,
  evidence_count, error_count,
  status: ok | error | interrupted | running
}

AuditNode {
  stage_id, node_exec_id, run_id,
  node_name, seq, plan_round, step_index, attempt,
  phase, status, started_at, ended_at, latency_ms,
  route, route_reason, decision_summary,
  evidence_count, citation_count,
  context_flags: string[],
  error, content_source
}
```

派生规则：

- `plan_round`：从 run 开始到当前节点，已完成的 `planner` 次数；第一个 `planner` 之前为 0，初始计划为 1，重规划后递增。
- `step_index`：优先取 `executor.output_json.step_index`；`tools/merge` 继承前一个 `executor` 的值；缺失时按 `executor.seq` 兜底并标 `step_index=null`。
- `kind=decision`：`element_assess`、`planner`、`replan_check`。
- `kind=step`：同一 `plan_round + step_index` 的 `executor/tools/merge` 归为一组。
- `kind=hitl`：`ask_element`、`field_clarify`、`risk_confirm`、`docx_confirm`、`mid_clarify`、`degrade_confirm`、`budget_confirm` 等。
- `kind=answer`：`finalize`、`chitchat`。
- 节点排序仍按 `started_at, seq`；组内保持真实执行顺序。

### EvidenceBoard

```text
EvidenceBoard {
  tool_calls: ToolCallEvidence[]
  documents: DocumentEvidence[]
  citations: CitationEvidence[]
  gaps: EvidenceGap[]
}

ToolCallEvidence {
  span_id, stage_id, tool, args_digest,
  status, latency_ms,
  counts: {laws, cases, memory, web},
  result_ref: string
}

DocumentEvidence {
  evidence_id, kind: law | case | memory | web,
  title, score, verdict,
  source_span_id, source_stage_id,
  used_in_analysis: bool | null,   // null = 旧 run 无关联字段，未知
  used_in_answer: bool | null
}

CitationEvidence {
  label, type: law | case,
  evidence_ids: string[],
  resolved: bool
}

EvidenceGap {
  code: retrieved_not_used | cited_without_source | no_evidence,
  label, stage_id?, evidence_id?
}
```

关联规则：

- `evidence_id = sha1(run_id:kind:doc_key)[:16]`（doc_key 为案例编号/法条名/记忆键等稳定键，不含 span_id，重复检索同一文档保持同一 ID）。
- 工具 span 归到同 run 最近的 `tools` stage；tool span 的 `started_at` 可能比 `tools` 节点 span 早几毫秒（落在 `executor` 尾部），不能按包含区间归到 `executor`。
- 工具输出解析出文档后，写入 `minor_states.refs.evidence_ids`；`merge` 累积到 state 的 `evidence_ids`。
- `analyze_legal_issue` 使用证据时，把实际注入的 `evidence_ids` 写入 `refs.evidence_ids_used`。
- `finalize` 生成 citations 时，按 `label + content hash` 匹配证据，写入 `citations[].evidence_ids`；匹配不到则 `resolved=false`。
- 旧 run 没有 `evidence_id`：`documents` 仍从 span 输出推导，`citations` 保留标题但 `resolved=false`，并生成 `audit_partial` anomaly。
- `used_in_analysis/used_in_answer` 为 `null`（旧 run 未知）时不生成 `retrieved_not_used`，只标 unknown，避免把“没采集”误判成“没使用”。

### ContextMatrix

```text
ContextMatrix {
  segments: string[]  // task, current_query, critical_facts, history, evidence, constraints, memory
  nodes: ContextNode[]
  summary: {
    total_chars, truncated_segments: string[],
    history_turns: int | null,   // 从 trace_spans.state.messages 的 human 条数取；无 span 则 null
    memory_hits: int
  }
}

ContextNode {
  stage_id, node_name,
  segments: {
    [name]: {present, chars, hash, truncated, injected}
  },
  diff: {added: string[], removed: string[]},
  inherited_from_stage_id: int | null
}
```

规则：

- `segments` 来自 `minor_states.context_meta`，只保留 `{chars,hash,truncated}`，不复制正文。
- 段级截断由 `context_meta.truncated_segments`（路径列表，如 `history.recent`）定位；旧 meta 只有全局 `truncated` 布尔时不伪造段级标记，只在 anomaly 中表达。
- 没有 `context_meta` 的节点：继承最近一个带 meta 的节点的段信息，`injected=false`，`inherited_from_stage_id` 指向上游。
- `diff` 与同 run 内上一个带 meta 的节点比较 hash。
- `history_turns` 从 `context_meta.history.turns` 取；`memory_hits` 从 `context_meta.memory.hits` 取，缺失时回退 `tools` 输出的 `memory_count`。

### PerformanceBoard

```text
PerformanceBoard {
  total_ms, execution_ms, wait_human_ms, overhead_ms,
  slowest: [{stage_id, node_name, latency_ms, pct_of_execution}],
  errors: [{stage_id, node_name, error, retry}],
  tokens: {total, by_node: {stage_id: number}},
  retries: int,
  limit_hit: bool
}
```

- 节点耗时取 `stage_chain.latency_ms`，缺失按起止时间计算。
- 工具/LLM 耗时、tokens 取 `trace_spans.latency_ms/token_usage`，按 `run_id + name` 归到节点。
- `wait_human_ms` 取 run 链之间的空档。
- 慢点阈值来自 `/monitor/config`，不硬编码在前端。

### Anomaly

```text
Anomaly {
  code, severity: info | warn | error,
  title, detail,
  run_id?, stage_id?, evidence_id?
}
```

固定 code：

```text
node_error, tool_error, replan_triggered,
evidence_insufficient, citation_unresolved,
context_truncated, context_missing,
minor_missing, span_missing,
hitl_waiting, limit_hit, stale_run, audit_partial
```

`verdicts` 由 `anomalies` + 统计量生成，禁止页面自己再推导一套结论。

## API 契约

### 1. 审计视图（整条 run 链）

```http
GET /monitor/runs/{run_id}/audit-view?chain=1
```

- `chain=1` 默认，返回最多 3 层 resume 链；`chain=0` 只返回当前 run。
- 200：`AuditView`
- 404：run 不存在
- 503：监控库不可用，返回 `{detail: "监控数据暂不可用"}`，不返回伪空成功
- 响应体目标 < 200 KB；`documents` 最多 50 条，`tool_calls` 最多 200 条，长文本只给摘要。

### 2. 节点审计详情

```http
GET /monitor/stages/{stage_id}/audit-detail?include_raw=0
```

```text
AuditNodeDetail {
  node: AuditNode
  decision: {summary, reason, steps?, tool?, args_digest?, hitl?}
  evidence: {tool_calls, documents, citations, gaps}
  context: ContextNode
  calls: [{span_id, span_type, name, status, latency_ms, tokens}]
  raw?: {minor_state, span_input, span_output, span_state}  // 仅 include_raw=1
}
```

- 默认 `include_raw=0`，不返回 prompt/原始参数/完整 state。
- `include_raw=1` 仅用于本地审计，前端“原始数据”tab 才请求。

### 3. 增量更新（运行中）

```http
GET /monitor/runs/{run_id}/audit-view?after_stage_id={n}
```

- 返回新增/变化的 `timeline` 节点、`anomalies`、`verdicts` 和 `performance` 快照。
- 前端只在 `status=running` 时每 2 秒轮询；终态停止。
- v1 用增量轮询；SSE 留到后续版本，避免一次性改动传输层。

### 4. 兼容策略

- 保留 `/monitor/overview`、`/monitor/runs`、`/monitor/runs/{id}/stages`、`/timeline`、`/minor-states`、`/stages/{id}/detail`。
- 新页面只消费 `audit-view` / `audit-detail`；旧端点保留一个发布周期，便于回滚。
- 不新增表、不做 DDL；新增字段全部写入现有 JSONB：`output_json`、`context_meta`、`refs`、`metrics`。

## 采集补齐（不新增表）

| 缺口 | 写入位置 | 说明 |
|---|---|---|
| 计划轮次 | 由 `planner` stage 序列派生 | 无需写入 |
| 计划步骤 | `executor.output_json.step_index` 已有 | `tools/merge` 继承 |
| 决策摘要 | `minor_states.output_json.decision` | planner/executor/replan/HITL 各自写 |
| 证据 ID | `minor_states.refs.evidence_ids` | 工具输出解析后写入 |
| 分析使用证据 | `refs.evidence_ids_used` | `analyze_legal_issue` 注入上下文时写 |
| 引用关联 | `final_answer.citations[].evidence_ids` | finalize/chitchat 写 |
| 上下文覆盖 | `context_meta` 补到 executor/tools/merge | 无信封的节点继承并标 `injected=false` |
| tokens | `metrics.tokens` + `trace_spans.token_usage` | 从 span 汇总 |
| 工具耗时 | `trace_spans.latency_ms` | 已存在，补读取 |

全部为 JSONB 内新增键，旧行缺键时 builder 降级，不做回填迁移。

## 前端信息架构

### 路由与状态

- `/monitor` 保留。
- 深链：`/monitor#run=<run_id>&stage=<stage_id>&tab=<tab>`。
- 运行中轮询；终态停止。
- 首次加载只请求 `audit-view`；`audit-detail` 在点节点后按需请求。

### 五段式布局

| 段 | 组件 | 内容 | 交互 |
|---|---|---|---|
| 0 | `AuditRunHeader.vue` | run_id、会话、模式、状态、父 run、总耗时 | 复制 run_id、切换 run |
| 1 | `AuditVerdictBar.vue` | 流程/证据/上下文/性能四张结论卡 | 点击跳对应段落 |
| 2 | `AuditTimeline.vue` | 垂直时间轴；决策节点展开，执行步骤折叠为组；HITL 分隔条 | 点节点开抽屉 |
| 3 | `AuditEvidenceBoard.vue` | 工具调用 → 检索/评估 → 终答引用三段链 | 点证据定位节点/span |
| 4 | `AuditContextMatrix.vue` | 节点 × 上下文段矩阵；字数/hash/截断/差异 | 点单元格开上下文 tab |
| 5 | `AuditPerformancePanel.vue` | 慢点 Top 3、错误/重试、HITL 等待、tokens、limit_hit | 点条目定位节点 |
| 6 | `AuditNodeDrawer.vue` | 概览/决策/证据/上下文/调用/原始数据 | 右侧滑出，深链保持 |

### 视觉与交互原则

- 首屏不出现长文本；摘要单行，超出省略。
- `fallback_placeholder`、截断、缺 span、旧 run 不完整，必须有橙色/灰色标记。
- “原始数据” tab 默认折叠，展开前提示可能含案情与内部 prompt。
- 移动端不承诺；桌面 1280px 以上为主。

## 错误处理

| 场景 | 行为 |
|---|---|
| run 不存在 | 404 + 空态，不显示半页 |
| 监控库不可用 | 503 + 明确提示 |
| `minor_states` 缺失 | 合成占位节点，标 `minor_missing` |
| `trace_spans` 缺失 | 证据/调用 tab 显示 `span_missing`，节点仍可见 |
| 旧 run 无 evidence id | `audit_partial`；引用标未解析 |
| 响应过大 | 截断列表并标 `truncated`；全文走 `include_raw=1` |
| 轮询失败 | 保留上次快照，顶部标“数据可能过期”，不清空页面 |

## 安全与隐私

- `AuditView` 不包含 prompt、完整工具参数、`AgentState`。
- `audit-detail` 默认不返回 raw；只有显式 `include_raw=1` 才返回。
- 原文继续只存在 `trace_spans`，不复制到新表或导出文件。
- 审计导出（若后续需要）默认排除 `final_prompts`、`prompts_record`、工具原始 args，沿用 transcript 导出的脱敏先例。
- 监控页仍建议只在本机访问；演示时不展开“原始数据”tab。

## 测试策略

### 后端单元

`tests/test_audit_view.py`：

- 计划轮次/步骤分组：27 stage 的 run 折叠后组数正确。
- HITL 链：3 层 `parent_run_id` 串链、等待时长计算。
- 证据映射：工具 span → document → used_in_analysis → citation。
- 未解析引用：无 evidence id 时 `resolved=false` + gap。
- 上下文继承：缺 meta 节点继承上游，diff 正确。
- 缺失行：minor 缺失、span 缺失、旧 run partial。
- verdict/anomaly：四类结论与 anomaly 一致。

### API 契约

- 200 结构符合 `AuditView`；`schema_version=1`。
- 404/503/空链。
- `include_raw=0` 响应中不出现 `prompt`、`final_prompts`、`span_state`。
- 响应体积上限与列表截断。

### 前端

- `npm run build` 通过。
- 深链刷新保持 run/stage/tab。
- 运行中轮询停止条件正确。
- 空态/降级/占位/截断均可见。

### 回归

- 现有 `/monitor/*` 端点行为不变。
- Agent 执行路径无改动；audit builder 只读。

## 分阶段实施计划

### P0：契约与夹具（0.5-1 天）

- 新增 `lawApp_LangGraph/FastAPI/audit_model.py`（Pydantic 模型）。
- 用 `MULTIFIX-1:225123432551` 生成 golden fixture。
- 写模型序列化/缺省值测试。

验收：模型可序列化，fixture 覆盖 27 stage 与 1 次 HITL 链。

### P1：只读 builder + 端点（2-3 天）

- 新增 `lawApp_LangGraph/audit_view.py`：分组、verdict、evidence、context、performance、anomaly。
- 新增 `GET /monitor/runs/{run_id}/audit-view`、`GET /monitor/stages/{stage_id}/audit-detail`。
- 旧端点保留。
- `tests/test_audit_view.py` 全绿。

验收：真实 run 返回结构完整；旧 run 标 `audit_partial`；404/503 正确。

### P2：采集补齐（1-2 天）

- 工具输出写 `refs.evidence_ids`。
- `analyze_legal_issue` 写 `refs.evidence_ids_used`。
- finalize/chitchat citations 写 `evidence_ids`。
- executor/tools/merge 补 `context_meta` 继承标记。
- planner/executor/replan 写 `output_json.decision.summary/reason`。
- tokens 汇总。

验收：新 run 引用 100% 可解析（有匹配证据时）；旧 run 不受影响。

### P3：前端五段式与抽屉（3-4 天）

- 新增 `AuditRunHeader/VerdictBar/Timeline/EvidenceBoard/ContextMatrix/PerformancePanel/NodeDrawer`。
- `MonitorView.vue` 接入 `audit-view`；保留旧时间轴组件复用。
- 深链、加载/空态/降级。
- 功能开关 `VITE_MONITOR_AUDIT`（默认开）。

验收：一次 run 可完成“结论 → 时间轴 → 证据 → 上下文 → 性能”走查；首屏无原始 JSON。

### P4：实时与打磨（1-2 天）

- 增量轮询与终态停止。
- 响应截断、慢查询优化、错误提示。
- 演示脚本与页面文案。

验收：运行中 2 秒内看到新节点；终态不再请求。

### P5：切换与回滚（0.5 天）

- 新旧视图双跑一轮。
- 记录 audit-view p95、payload 大小。
- 保留旧端点与开关，出问题可切回。

验收：无回归；可一键回滚。

## 验收标准

1. 打开一次 run，10 秒内回答：证据链是否成立、上下文是否正确、性能与异常在哪。
2. 27 stage 的 run，折叠后主路径组数 ≤ 12，默认不展开执行细节（决策/证据/终答组保持可见）。
3. 新 run 的每个终答引用可点击到证据，再到工具调用/节点；旧 run 明确标“部分可审计”。
4. 首屏不出现 prompt、完整工具参数、`AgentState`；原始数据 tab 默认关闭。
5. `audit-view` 响应 < 200 KB，本地 p95 < 500 ms（30 节点/50 证据量级）。
6. 现有 `/monitor/*` 与 Agent 执行无回归。
7. 演示可按五段顺序讲完，3 分钟内完成一次完整走查。

## 风险与取舍

| 风险 | 应对 |
|---|---|
| 证据 ID 需要改工具输出，可能影响现有消费者 | 只加 JSONB 键，不改原有字段；builder 对缺失键降级 |
| 旧 run 审计不完整 | 明确 `audit_partial`，不承诺回填历史 |
| 时间轴仍可能很长 | 按计划步骤折叠，决策节点优先 |
| 页面信息密度高 | 五段式 + 抽屉分层，首屏只留摘要 |
| 原始数据泄露案情 | 默认不返回 raw；演示不展开；导出脱敏 |
| 新增端点增加查询压力 | 单 run 聚合、列表上限、索引已有；必要时缓存 |

## 待用户确认

1. 分组口径：按“计划步骤 + 计划轮次”分组（推荐），还是按时间连续段分组。
2. 是否接受 P2 的采集补齐（新增 JSONB 键，不改表结构）。
3. 是否保留旧 `/monitor/*` 端点一个发布周期。
4. 实时更新 v1 用 2 秒增量轮询（推荐），SSE 后置。
