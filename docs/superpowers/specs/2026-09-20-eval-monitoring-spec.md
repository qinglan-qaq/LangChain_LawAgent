# 类 LangSmith 观测评控平台 — 需求规格(Spec)

- 日期: 2026-09-20(访谈跨 2026-09-21)
- 状态: 定稿(四轮 grilling 访谈 + brainstorming 细化轮完成,20 项决策 + 8 项细化全部经用户确认)
- 底稿: [2026-09-20-eval-monitoring-design.md](2026-09-20-eval-monitoring-design.md)(architectural brainstorm 产出,已提交)
- 决策出处: grilling 四轮(R1 轮 Q1-Q17)+ brainstorming 细化轮(R1-R8),全部经用户逐项确认;本规格按 to-spec 模板格式组织

## Problem Statement

Agent 已经能跑通全链路(咨询/HITL/流式恢复),但我们对它的运行**看不见**:

1. **检索瘸腿无人知**: 案例检索实际返回空(命名空间错配 + 后端指向空表),agent 靠法条+联网搜索硬撑,而现有手段发现不了 —— 没有任何指标或看板能暴露"检索命中率是 0"。
2. **节点级黑盒**: 一次咨询里每个图节点消耗了什么内容、产出了什么、执行后状态变成什么样、跑了多久、烧了多少 token —— 全部不可见,只能翻日志拼。
3. **线上问题无法回放**: 用户反馈"答案不对"时,audit 表有事件、日志有 JSON 行,但拼不出一次完整运行的瀑布视图。
4. **基础设施脆弱性不可观测**: PG 掉线时 `/sessions` 直接 500,没有降级也没有告警信号。

用户(开发者本人)需要一套类 LangSmith 的观测系统: 每次运行、每个节点自动留痕(内容/状态/时间/token),可查可回放可对比;**代码侧以装饰器形式按需插桩**,不侵入节点内部逻辑。**本期纯观测,不做任何自动判定(judge/断言均否)。**

## Solution

在现有 PG 单库上自建轻量观测系统,分阶段交付(顺序执行不卡工期):

- **P0 检索修复(前置)**: 命名空间参数化 + 切 Pinecone 后端,让 agent 恢复案例检索能力;会话写入加降级包装,PG 掉线不再 500。
- **P1 Trace 落库(本期核心)**: `trace_runs`/`trace_spans` 两表 + **节点装饰器插桩**(`@traced` 类机制): 需检测的节点/工具/LLM 调用被装饰器包裹,自动记录**运行时间、调用的函数、执行前后成果、执行结果、消耗 token 值**;执行后完整 state 快照由 values 流对齐回填。全文 JSONB 落库。
- **P2 评测数据集与观测指标(无判定)**: golden set(30 条自动检索 + 5 条人工审核 e2e) + 规则指标观测(hit_rate@5/MRR@10/流程指标/时延/token 基线,首批不设目标线)。e2e 5 条只跑留 trace 供人工查看。`--baseline` 对比能力顺带获得。
- **P3 监控 UI(单页堆叠)**: 前端 `/monitor` 单一路由页,上下堆叠联动 — 指标汇总区 → 运行列表(vxe-table) → 点击行展开详细 span 瀑布流。
- **P4 回归门禁**: **暂缓**。手动 `--baseline` diff 能力保留;红绿拦截/hook 后续视实际使用价值再定。

## User Stories

**开发者 — 插桩与观测(Trace,本期核心)**

1. As a 开发者, I want 用**装饰器**标记需要检测的节点函数, so that 插桩按需启用、零侵入(节点内部逻辑一行不改)。
2. As a 开发者, I want 装饰器自动记录节点**运行时间、调用的函数名、执行前后成果、执行结果**, so that 加一行装饰器 = 该节点全维度留痕。
3. As a 开发者, I want 节点执行后的**完整状态数据**随 span 落库(全量快照), so that 我能回放 state 逐步演化(检索结果从空到有、计划从 3 步变 6 步)。
4. As a 开发者, I want 每次 ask/resume 运行自动落库为一条 trace_run 及其全部 spans, so that 我随时能回看任意一次会话的完整执行过程。
5. As a 开发者, I want span 携带消耗的输入内容与产出输出全文, so that 改 prompt 时我能看到模型真实看到了什么。
6. As a 开发者, I want span 记录消耗 token 值, so that 我能算出每次咨询的 LLM 成本基线。
7. As a 开发者, I want 运行状态(ok/interrupted/error/degraded)与中断类型记录在 run 级, so that 我能统计中断率并区分用户主动跳过与异常中断。
8. As a 开发者, I want trace 写库失败只记 ERROR 不阻塞主流程, so that 观测旁路永远不会弄瘫业务链路。

**开发者 — 评测数据集与观测指标(无判定)**

9. As a 开发者, I want 一条命令跑 retrieval 套件并输出 hit_rate@5/MRR@10 观测值, so that 命名空间错配这类断裂有量化暴露(首批纯观测不设线)。
10. As a 开发者, I want golden set 中 30 条检索用例从 Pinecone 真实语料分层抽样生成, so that 语料更新后数据集可脚本重建。
11. As a 开发者, I want golden set 中 5 条端到端用例由 agent 起草、我审核定标, so that 人工投入最小化且可控。
12. As a 开发者, I want e2e 5 条跑完自动留痕 trace(不做任何判定), so that 我能在监控页人工查看每条用例的完整执行过程。
13. As a 开发者, I want `--baseline` 对比模式输出两批观测指标 diff, so that prompts.py 改动前后有量化对照(手动跑,不拦截)。
14. As a 开发者, I want 评测运行与线上运行隔离(eval- 前缀 thread_id,不进 /sessions 列表), so that 评测流量不污染用户会话数据。
15. As a 开发者, I want 流程指标(clarify 轮数/replan 率/工具错误率/P50-P95 时延/token 成本)由 trace_spans 规则聚合, so that 零额外 LLM 成本监测流程健康度。

**开发者 — 监控 UI(单页堆叠)**

16. As a 开发者, I want 前端 `/monitor` **单页**内含运行列表(vxe-table,排序/筛选/行展开), so that 我不用写 SQL 就能排查"昨天那次异常会话"。
17. As a 开发者, I want 同页堆叠选中 run 的详细 span 瀑布流(每节点/工具/LLM 的函数名、时长条、前后成果、state 快照、token), so that 一次点击完成原本要拼 audit+日志+checkpoint 的回放。
18. As a 开发者, I want 同页堆叠指标看板(各批次观测指标与 baseline 对比趋势), so that 质量变化一眼可见。
19. As a 开发者, I want feedback 评分与 trace 指标 join 对齐展示, so that 我能验证"指标差的那批答案用户确实打分低"。

**HITL / 稳定性**

20. As a 开发者, I want semantic_confirm 判定结果作为 hitl span 落库, so that 我能回放查看每次语义判定现场。
21. As a 开发者, I want 中断(resume)也走同一 trace 通道(run_type=live_resume), so that HITL 全过程与首轮咨询在同一视图连续回放。
22. As a 开发者, I want PG 掉线时业务会话接口降级而非 500, so that Docker Desktop 抖动不直接打断用户体验。
23. As a 开发者, I want 检索链路修复后 retrieve_legal_knowledge 实测命中案例, so that agent 的结论恢复"案例+法条+联网"三路支撑。

## Implementation Decisions

1. **自建轻量系统,复用 PG 单库**(否决 Langfuse OSS / LangSmith 云,理由同底稿 §2 D1)。trace 表 schema 按 Langfuse 兼容子集设计,保留迁移退路。本地本机使用,不加访问保护。P0-P4 全做,不卡工期,保序执行。
2. **数据模型 — 三表**:

   ```sql
   trace_runs(run_id PK, session_id, run_type live_ask|live_resume|eval, mode,
              status ok|interrupted|error|degraded, query, final_answer,
              metrics JSONB, started_at, ended_at)
   trace_spans(id BIGSERIAL PK, run_id FK,
              span_type TEXT NOT NULL,  -- node | tool | llm | hitl
              name,                    -- 装饰器捕获的被调用函数名
              status,                  -- 节点执行结果 ok|error
              input JSONB, output JSONB,   -- 执行前后成果: 入参 state / 返回结果
              state JSONB,            -- 节点执行后的完整状态数据(全量快照,values 流对齐回填)
              latency_ms, token_usage JSONB, started_at)
   eval_runs(id PK, dataset retrieval|e2e, label,
             metrics JSONB, cases JSONB, created_at)
   ```

   run→span 层级与字段命名采 Langfuse 习惯(B 计划迁移退路)。**全文存储不截断,无限保留**(单机 PG,TOAST 压缩兜住重复存储)。token_usage 读 langchain usage_metadata,取不到记 null(可选遥测字段)。
3. **插桩方式 = 装饰器,三层全包**(替代底稿 D4 status 总线双写): 装饰器工厂统一包 **图节点 / 工具函数 / LLM 调用** 三层,span_type 对应 node|tool|llm;hitl 事件由 API 层记录(非函数不可装饰)。装饰器自动记录 — 运行时间(perf 计时)、调用的函数(fn 名)、前后成果(入参/返回值)、执行结果(异常捕获,**异常原样透传不吞**,符合"有错误直接报"约定)。未装饰的函数零开销不落库。装饰器负责观测数据(攒批落库),status 总线继续管 SSE 实时流,两者互不替代;节点内 `_publish_status` 现有插桩不动。
4. **run 上下文传递 = contextvars**(细化 R1): `ContextVar` 持有当前 run_id —— `_run_sse`/eval runner 在 astream 前 set,装饰器读;asyncio 每任务独立,并发安全。未 set(如单测直接调节点)时 span 攒进"游离缓冲"不落库,不报错不阻塞。
5. **state 全量快照来源 = values 流对齐回填**(细化 R2): 装饰器只记 入参/返回值/时延/token/结果(装饰器物理上拿不到合并后全量 state,自行合并需复刻 reducer 语义,易错);span.state 列由 `_run_sse` 已订阅的 astream values 流按节点名对齐回填 —— 节点后全量 state 现成可得,零新通道。
6. **LLM 层包装位置 = 两个 llm 工厂返回处**(细化 R3): `get_planner_llm()`/`get_executor_llm()` 返回包装过的 Runnable(拦截 ainvoke/astream),单点覆盖全部 LLM 调用(含 `with_structured_output`/bind_tools 生成的新 Runnable/streaming CoT 调用),自动捕获 prompt/completion/usage_metadata/时延;不逐函数加装饰器。
7. **run_id 生成规则**(细化 R4): live 运行 `run_id = f"{session_id}:{started_at:%H%M%S%f}"`(时间戳天然唯一、可读);eval 运行 `eval:{dataset}:{case_id}`。监控页按 session_id 分组即连续回放。
8. **落库点 = `_run_sse` finally**(细化 R5): 含正常/interrupt/error 三路统一收尾,run 状态对应 ok/interrupted/error,spans 全量落;写库失败 try/except 记 ERROR 放行,不阻塞业务。
9. **trace_runs.metrics 内容**(细化 R6): 收尾时聚合填入 — 节点数/工具调用数/LLM 调用数/总时延/总 token(prompt+completion)/clarify 轮数;监控页列表直接展示,不实时聚合 spans。
10. **HITL run 拆分**: 中断 run 记 interrupted,resume 记新 run(run_type=live_resume),同 session_id 关联;监控页按 session 聚合连续回放。
11. **本期不做任何自动判定**: 无 LLM judge、无规则断言、无红绿门禁;e2e 只跑留痕供人工查看;judge 与门禁设计保留在底稿作后续参考。P4 门禁整项暂缓,手动 `--baseline` diff 能力保留。
12. **Golden set 35 条双来源**: 30 条自动 —— Pinecone 694 块按 case_cause/year/chunk_index=0 分层抽样,LLM 从 chunk_text 提炼"当事人视角问题",该案例自身即 hit ground truth;5 条人工 — agent 起草完整咨询场景,用户审核定标,场景覆盖(细化 R8): ①财产分割(民法典 1062/1087)②抚养权归属 ③精神损害赔偿(1091)④出轨转移财产追偿(1092)⑤信息不足触发 clarify 流程。存 data/eval/*.jsonl(数据文件不进库,可重生成)。
13. **指标(首批纯观测,不设目标线)**: 检索 hit_rate@5 / MRR@10;流程 clarify 轮数 / replan 率 / 中断率 / 工具错误率;时延 P50/P95;token 成本;真实基线跑出后,后续批次再决定是否设线。
14. **Eval runner**: `scripts/run_eval.py --suite retrieval|e2e|all --label <git_sha> --baseline <label>`;每 case 起 `run_type=eval` 运行,直调 graph 不走 HTTP(便于并发与 thread_id 隔离),结果落 eval_runs;runner 只产出观测数值与 trace,无判定层。eval 用 `eval-` 前缀 thread_id,查询处过滤 run_type 使 eval 会话不进 /sessions 用户列表。
15. **监控 UI = 前端单一 `/monitor` 路由页,上下堆叠联动**: 自上而下 — 指标汇总卡片区 → 运行列表(vxe-table) → 点击行在本页下方展开**详细 span 瀑布流**(时长条 + 每节点/工具/LLM 的函数名、前后成果、state 快照、token 折叠面板),锚点滚动定位;同 session 多 run(含 live_resume)分组连续展示。表格用 **vxe-table**(开箱即用虚拟滚动/排序/筛选/行展开),时延瀑布条 CSS 自绘。REST 契约: `GET /admin/traces`(列表过滤,支持按 session 聚合)、`GET /admin/traces/{run_id}`(含 span 瀑布)、`GET /admin/evals`(批次指标)。**端点即导出,不做导出按钮**。
16. **监控页刷新 = 手动刷新按钮 + 进入页自动拉一次**(细化 R7),不做轮询空转,不做实时推送。
17. **audit 并存双写**: audit 表现有用途(HITL 决策等)不动,trace 承担运行级观测,重叠接受。
18. **P0 前置修复**(评测的前提): PINECONE_NAMESPACE 环境变量参数化(默认 law_cases,.env 设 Law_test_namespace);`.env RETRIEVER_BACKEND=pinecone`;upsert_session 包 `_safe_*` 降级;checkpoint 断连自愈检查。
19. **DDL 幂等**: trace/eval 表进 db.py ensure_tables(CREATE TABLE IF NOT EXISTS),与既有业务表同机制。
20. **阶段依赖**(不卡工期,保序): P0→P2(检索数据集前提),P1→P3(trace 前提),P2/P3 可并行。P4 暂缓。

## Testing Decisions

- **好测试只测外部行为**: 指标计算纯函数(hit_rate/MRR/聚合)直接断言数值;装饰器测试断言"被装饰函数执行后收集器收到 span(函数名/时延/前后成果/结果)",不测收集器内部;trace 落库测试断言"跑一次图后表里出现对应 run/span 行(含 state 快照/latency/token)",不测收集器实现。
- **测试接缝(seams)**:
  1. **最高接缝 — `scripts/run_eval.py` 进程级调用**(P2): 对真实 graph 跑小型 suite,断言 eval_runs 出行、trace 留痕完整。先例: scripts/gen_nb07.py 同为"真实服务+真实 LLM"模式。
  2. **REST 接缝 — `/admin/traces` 端点**(P3): HTTP 层断言列表/详情返回,复用 test_smoke 的 FastAPI TestClient 模式。
  3. **表接缝 — ensure_tables DDL**(P1): 幂等重复执行不报错,真实 PG,不可用时显式 SKIP(不 mock)。
- **装饰器本身用最普通单元测试覆盖**(纯 Python,无 PG 依赖): 计时/异常透传(装饰后节点异常必须原样抛出)/函数名捕获/run 上下文缺失时走游离缓冲。
- **不新增接缝**: span 收集器行为通过端到端结果间接验证。
- **存量不动**: 现有 pytest 不改动;新增测试独立文件。

## Out of Scope

- **LLM-as-judge 评测**(Round 1 Q2 明确不做)
- **e2e 用例自动判定/规则断言**(Round 2 Q9: DO NOT NEED JUDGE)
- **P4 回归门禁与红绿拦截**(Round 2 Q11: 暂缓)
- **首批指标目标线**(Round 2 Q10: 首跑纯观测)
- trace 数据清理(Round 3 Q13: 无限保留,策略后续再说)
- 运行数据导出按钮(Round 3 Q16: 端点即导出)
- 监控页访问控制 / 多用户权限(Round 1 Q4: 本地本机)
- 监控页实时推送与轮询(R7: 手动刷新)
- 在线实验(A/B 分流)、自动 prompt 优化(DSPy 类)
- 分布式追踪(单机单进程)
- Langfuse OSS 迁移(仅 schema 兼容退路)

## Further Notes

- 评测旁路原则: trace/eval 任何失败不得阻塞业务;业务本身失败显式报错,不做静默兜底。
- pg_stat n_live_tup 显示陈旧值(实测 law_vector 显示 0 实有 1526 行),监控页计数以精确 count 为准。
- 前端事实: 引 vxe-table 为新依赖(当前无表格库,Inspira UI 无 data grid,reka-ui 为 headless 基件)。
- token 事实: 代码目前不采集 LLM token;langchain 消息自带 usage_metadata,装饰器/包装层新增读取。
- 插桩对象事实: 15 个图节点(ingest/risk_gate/element_assess/ask_element/planner/executor/tools/merge/replan_check/mid_clarify/hitl_degrade/hitl_budget/replanner/finalize/chitchat),节点函数 `async def (state, config)`;LLM 经两个单例工厂分发,`with_structured_output(...).ainvoke(...)` 散布各节点。
- 决策记录: 四轮访谈逐项决策(20 项)与细化轮(8 项)全部经用户确认;访谈过程决策表见本文件 git 历史(885868c..定稿提交)。
