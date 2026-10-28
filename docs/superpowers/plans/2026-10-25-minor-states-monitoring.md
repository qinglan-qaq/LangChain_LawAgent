# minorStates 内容监控 + 记忆/上下文/要素补全 — 实施计划

- 对应设计: [2026-10-23-minor-states-monitoring-design.md](../specs/2026-10-23-minor-states-monitoring-design.md) v2
- 日期: 2026-10-25
- 目标: 16 节点执行级内容监控(stage_chain + minor_states 单事务)、JSON 上下文信封、flash 结构化记忆压缩、element_fill 节点、闲聊人设、监控页竖向时间轴与占比可视化
- 不变约束: 观测旁路失败不阻塞业务; `STAGE_ZIPPER=0` 全关; `MINOR_STATES=0` 只关内容层; 现有 250 pytest 零回归

## 全局约束

- 所有新增配置进 `lawApp_LangGraph/config.py`, env 同名大写覆盖。
- `minor_states` 只存内容, 生命周期字段全读 `stage_chain`; `stage_id` 是主键+外键。
- MonitorWriter 单队列、单消费者、每批单事务; 抛错只 warning。
- `build_context` 键顺序固定: `current_query → critical_facts → history → evidence → task → constraints`。
- 不可压缩字段只从 state 读取, 不读 summary。
- 前端不新增依赖, 复用 `reka-ui` + Tailwind; 删除甘特与抽屉。
- 每任务先写失败测试, 再实现, 最后跑相关测试; 完成后全量回归 + `vite build`。

## 文件结构

| 文件 | 动作 |
|---|---|
| `lawApp_LangGraph/config.py` | minor/memory/context/element_fill 配置 |
| `lawApp_LangGraph/state.py` | `conversation_memory`、`pending_fill_keys`、`element_fill_rounds`、`PlanStep.args` |
| `lawApp_LangGraph/minor_state.py` | 新增: 16 节点投影/清洗/截断 + MonitorWriter |
| `lawApp_LangGraph/context.py` | 新增: JSON 上下文信封 + 段预算 |
| `lawApp_LangGraph/memory.py` | 新增: 记忆摘要/压缩/渲染 |
| `lawApp_LangGraph/db.py` | DDL、`start_trace_run`、minor 查询助手 |
| `lawApp_LangGraph/tracing.py` | writer 挂接、指标修正、context_meta/parent_run_id |
| `lawApp_LangGraph/LangGraph_lawApp.py` | 节点接入 context/memory、element_fill、路由、图 |
| `lawApp_LangGraph/prompts.py` | 模板改 `{context_json}`、闲聊人设、伪工具说明 |
| `lawApp_LangGraph/FastAPI/model.py` | ResumeRequest.parent_run_id + 监控模型 |
| `lawApp_LangGraph/FastAPI/api.py` | start_trace_run、4 新端点、runs/overview 扩展、route 回填 |
| `frontend/src/api.js` | timeline/detail/minor-states/config 封装 |
| `frontend/src/views/MonitorView.vue` | 重写为时间轴 + 下方详情 |
| `frontend/src/components/MonitorTimeline.vue` 等 5 个 | 新增 |
| `frontend/src/components/GanttTimeline.vue` / `StageDetailDrawer.vue` | 删除 |
| `tests/test_minor_state.py` 等 | 新增/扩展 |

## Task 1: 配置 + 投影纯函数

**测试** `tests/test_minor_state.py`
- `project_input/project_output` 对 16 节点返回 `(str, dict)` 不抛; 允许空文本的节点只有约定项。
- 输入含 `messages/prompts_record/final_prompts/rag_documents[].chunk_text` 时不进入投影; HTML/base64 丢弃。
- 超 2000 字截断并返回 `truncated_chars`; JSON 超 64KB 收缩并标记。
- 投影内部异常返回 `projection_error` 不抛。

**实现**
- `config.py` 增 §13 全部字段。
- `minor_state.py`: `_get`, `_clip`, `_sanitize`, `project_input`, `project_output`, `NODE_CONTRACT`; 白名单/黑名单/裁剪按 spec §四。

**验证** `pytest tests/test_minor_state.py -q`

## Task 2: DB + MonitorWriter

**测试** `tests/test_minor_writer.py`(真实 PG, 无 PG skip)
- DDL 幂等; `minor_states` 1:1 约束; open/close 同事务行数一致。
- 队列满不抛; 消费者异常后继续; `parent_stage_id` 关联; `MINOR_STATES=0` 只写 stage。
- `start_trace_run` 插入 running, `insert_trace_run` 收尾更新。

**实现**
- `db.ensure_tables`: `minor_states` DDL + `trace_runs` 增列 + 索引。
- `db.start_trace_run`、`update_trace_run_current_node`。
- `minor_state.MonitorWriter`: `enqueue_open/close/route`, `drain`; `(run_id,node,seq)→stage_id` 映射; 批量 20 / 200ms。

**验证** `pytest tests/test_minor_writer.py tests/test_stage_chain.py -q`

## Task 3: @traced 挂接 + resume 关联 + route

**测试** `tests/test_tracing_minor.py`
- 节点成功/异常/interrupt 三类 close 的 status/phase 正确。
- `parent_run_id` 首个节点 `attempt=2`, `parent_stage_id` 命中 interrupted 行。
- route 回填只写上一已闭行。

**实现**
- `RunContext`: `parent_run_id`, `context_meta`, `attempt_bumped`。
- `@traced` 改走 writer, 保留旧 `open_stage/close_stage` 助手。
- `_run_sse`: `start_trace_run`; updates 流维护 last_node 并 `enqueue_route`; finally `await writer.drain()`。
- API resume 接收并校验 `parent_run_id`。

**验证** `pytest tests/test_tracing_minor.py -q`

## Task 4: 指标修正

**测试** `tests/test_tracing_metrics.py`
- `total_latency_ms` 只 node; `wall_clock_ms`; `clarify_rounds` 只 ask_element; mid/element_fill 独立; rag 优先 relevance; `limit_hit_reasons`。

**实现** `RunContext.metrics()` + `flush_run`; `trace_runs` metrics 写修正值。

**验证** `pytest tests/test_tracing_metrics.py tests/test_score.py -q`

## Task 5: JSON 上下文信封

**测试** `tests/test_context.py`
- 键顺序; 空段省略; 段预算截断; 不可压缩字段只来自 state; `current_query` 首、`constraints` 尾; meta 写入 RunContext。

**实现**
- `context.py`: `build_context(state, purpose)`、`render_context`, purpose→预算表。
- 迁移 `planner/executor/replan_check/finalize/element_assess/chitchat` 模板为 `{context_json}`。
- `_query_with_supplements` 保留旧行为, 内部复用 `build_context(..., "generic")` 的文本渲染。

**验证** `pytest tests/test_context.py tests/test_chat_memory.py -q`

## Task 6: 记忆与 HITL 回答

**测试** `tests/test_memory_compressor.py`
- schema 校验失败保留旧 memory; 字段级替换; `compressed_until_turn` 幂等; 三源输入含 HITL 原文。
- `_recent_history`: 跳过 Tool/System; 按轮次; 老 checkpoint 回退。

**实现**
- `memory.py`: `maybe_compress`, `memory_digest`, `render_recent`, `apply_hitl_answer`.
- `state.conversation_memory`; async ingest wrapper 惰性压缩(保留同步 `ingest_node` 兼容测试)。
- `ask_element/mid_clarify/element_fill` resume 写 `[补充]` HumanMessage + `user_supplements` + facts。
- `ingest` 清空原文前写 `retrieval_digest`。

**验证** `pytest tests/test_memory_compressor.py tests/test_chat_memory.py tests/test_hitl_single_question.py -q`

## Task 7: element_fill 节点

**测试** `tests/test_element_fill.py`
- 伪工具步骤 → `pending_fill_keys` → 路由 `element_fill`; resume 推进且 element 置 known; 达 2 轮不中断; 无缺失直接推进。

**实现**
- `PlanStep.args`; `_normalize_plan` 保留伪工具; planner 工具清单登记。
- executor 伪工具短路; `route_after_executor` 新分支; `element_fill_node`; 图 16 节点 + 边。

**验证** `pytest tests/test_element_fill.py tests/test_smoke.py -q`

## Task 8: 闲聊人设

**测试** `tests/test_persona_prompt.py`
- CHITCHAT 含"冷静务实/身份诚实/12338", 不含 `Kim Wexler/Domain Expertise`; finalize 仍含全量人设。

**实现** `prompts.CHITCHAT_PERSONA_BLOCK` + `CHITCHAT_PROMPT` 注入 `{persona}`/`{context_json}`。

**验证** `pytest tests/test_persona_prompt.py -q`

## Task 9: API 端点与模型

**测试** `tests/test_monitor_api_v2.py`
- timeline(含 chain 最多 3 层/占位卡/divider)、detail、minor-states、config; 404/503; runs 含 running/current_node; overview 新字段。

**实现**
- `model.py`: `MinorState/TimelineItem/TimelineComposition/TimelineRun/MonitorTimeline/MonitorConfig/MonitorStageDetail`。
- `api.py`: 4 端点 + runs/overview SQL 扩展; PG 异常降级。

**验证** `pytest tests/test_monitor_api_v2.py tests/test_monitor_api.py -q`

## Task 10: 前端

**实现**
- `api.js` 新封装。
- `MonitorTimeline.vue` / `TimelineCompositionBar.vue` / `MinorStateCard.vue` / `MinorStateDetail.vue` / `ContextMetaPanel.vue`。
- `MonitorView.vue`: 运行选择 → timeline → 下方详情; 2s 增量轮询; 深链 `#run=&stage=`; 过滤器。
- 删除甘特/抽屉; RAG 分数读 `/monitor/config`。

**验证** `cd frontend && npm run build`; 手动验收清单见 spec §十四。

## Task 11: 回归与文档

- `pytest -q` 全量零失败(真实 PG 可用时不 skip)。
- README 中英同步监控/上下文/记忆变更。
- spec 追加执行记录与偏差。
