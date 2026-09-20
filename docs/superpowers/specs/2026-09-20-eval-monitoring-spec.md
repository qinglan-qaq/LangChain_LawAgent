# 类 LangSmith 观测评测平台 — 需求规格(Spec)

- 日期: 2026-09-20
- 状态: 待评审(由 to-spec skill 合成,无访谈;评审后走 writing-plans 出实施计划)
- 底稿: [2026-09-20-eval-monitoring-design.md](2026-09-20-eval-monitoring-design.md)(architectural brainstorm 产出,已提交)
- 发布方式偏差: 本仓库未配置 issue tracker / triage 词汇表(需 `/setup-matt-pocock-skills`),规格按仓库惯例发布于 docs/superpowers/specs/,待 tracker 就绪后可迁移

## Problem Statement

Agent 已经能跑通全链路(咨询/HITL/流式恢复),但我们对它的质量**既看不见也证不了**:

1. **检索瘸腿无人知**: 案例检索实际返回空(命名空间错配 + 后端指向空表),agent 靠法条+联网搜索硬撑,而现有手段发现不了这一点 —— 没有任何指标或看板能暴露"检索命中率是 0"。
2. **改 prompt 无安全网**: prompts.py 每次修改后,只能靠人肉 E2E 点一遍判断有没有退化,没有可对比的量化基线。
3. **线上问题无法回放**: 用户反馈"答案不对"时,audit 表有事件、日志有 JSON 行,但拼不出一次完整运行的瀑布视图(哪个节点慢、哪个工具错、LLM 看到了什么输入输出了什么)。
4. **基础设施脆弱性不可观测**: PG 掉线时 `/sessions` 直接 500,没有降级也没有告警信号。

用户(开发者本人)需要一套类 LangSmith 的观测+评测系统: 每次运行留痕可查、每次变更可评测可对比、退化可门禁拦截。

## Solution

在现有 PG 单库上自建轻量观测评测子系统,分五个阶段(P0-P4)交付:

- **P0 检索修复(前置)**: 命名空间参数化 + 切 Pinecone 后端,让 agent 恢复案例检索能力;会话写入加降级包装,PG 掉线不再 500。
- **P1 Trace 落库**: `trace_runs`/`trace_spans` 两表,复用 Batch B status 总线插桩点,每次 ask/resume 运行自动留痕(节点/工具/LLM/HITL 四类 span)。
- **P2 评测流水线**: 40 条 golden set(30 自动 + 10 人工) + DeepSeek-as-judge 双盲评测 + 指标聚合落库,跑出并存档首批基线。
- **P3 监控 UI**: 前端 `/monitor` 路由 —— 运行列表 → span 瀑布 → 指标看板 → feedback 对齐。
- **P4 回归门禁**: prompts.py 变更必跑 e2e suite 对比 baseline,hit_rate/幻觉率退化即红。

## User Stories

**开发者 — 观测(Trace)**

1. As a 开发者, I want 每次 ask/resume 运行自动落库为一条 trace_run 及其 spans, so that 我随时能回看任意一次会话的完整执行过程。
2. As a 开发者, I want span 记录节点/工具/LLM/HITL 四类事件及各自时延, so that 我能定位一次慢响应到底是检索慢、LLM 慢还是工具慢。
3. As a 开发者, I want 查看某次运行中每个 LLM 调用的实际输入输出(截断快照), so that 改 prompt 时我能看到模型真实看到了什么。
4. As a 开发者, I want 运行状态(ok/interrupted/error/degraded)与中断类型记录在 run 级, so that 我能统计中断率并区分用户主动跳过与异常中断。
5. As a 开发者, I want trace 写库失败只记 ERROR 不阻塞主流程, so that 观测旁路永远不会弄瘫业务链路。
6. As a 开发者, I want token 用量(prompt/completion)随 span 记录, so that 我能算出每次咨询的 LLM 成本基线。

**开发者 — 评测(Eval)**

7. As a 开发者, I want 一条命令跑评测套件并输出指标摘要表, so that 我不需要手点 E2E 就知道当前系统质量。
8. As a 开发者, I want 检索质量指标(hit_rate@5/MRR@10)以真实 Pinecone 语料为 ground truth, so that 命名空间错配这类断裂有回归防护。
9. As a 开发者, I want 端到端指标(法条引用正确性/幻觉率/人设一致性)由 LLM judge 自动评分, so that 40 条用例的人工评审成本降到只维护锚定样本。
10. As a 开发者, I want judge 采用双盲 + 5 条人工锚定样本校验, so that judge 本身漂移时整批判次作废重跑而不是污染基线。
11. As a 开发者, I want `--baseline` 对比模式输出两批指标 diff, so that prompts.py 任何改动都有量化前后对照。
12. As a 开发者, I want 评测运行与线上运行隔离(eval- 前缀 thread_id,不进 /sessions 列表), so that 评测流量不污染用户会话数据。
13. As a 开发者, I want judge 失败或超时显式记 judge_error、完成率 <80% 判批失败, so that 评测结果永远可信,没有静默兜底。
14. As a 开发者, I want 流程指标(clarify 轮数/replan 率/工具错误率/P50-P95 时延)由 trace_spans 规则聚合, so that 不需要额外 LLM 调用就能监测流程健康度。
15. As a 开发者, I want golden set 可由脚本从 Pinecone 分层抽样再生成, so that 语料更新后数据集可重建而不靠手工维护。

**开发者 — 监控 UI**

16. As a 开发者, I want 前端 `/monitor` 页浏览历史运行(按状态/类型/时间过滤), so that 我不用写 SQL 就能排查"昨天那次异常会话"。
17. As a 开发者, I want 点开一条 run 看到 span 瀑布视图, so that 一次点击完成原本要拼 audit 表 + 日志 + checkpoint 的回放。
18. As a 开发者, I want 指标看板展示各批次评测指标及 baseline 对比趋势, so that 质量变化一眼可见。
19. As a 开发者, I want feedback 评分与 trace 指标 join 对齐展示, so that 我能验证"自动指标差的那批答案用户确实打分低"。
20. As a 管理员, I want 监控页后续可加口令保护, so that 法律敏感数据不被未授权访问。

**HITL / 回放**

21. As a 开发者, I want semantic_confirm 判定结果作为 hitl span 落库, so that 我能回放 20 条标注样本统计语义判定的准确率(目标 ≥0.90)。
22. As a 开发者, I want 中断(resume)也走同一 trace 通道(run_type=live_resume), so that HITL 全过程与首轮咨询在同一视图连续回放。

**稳定性**

23. As a 开发者, I want PG 掉线时业务会话接口降级而非 500, so that Docker Desktop 抖动不直接打断用户体验。
24. As a 开发者, I want 检索链路修复后 retrieve_legal_knowledge 实测命中案例, so that agent 的结论恢复"案例+法条+联网"三路支撑。

## Implementation Decisions

1. **自建轻量系统,复用 PG 单库**(否决 Langfuse OSS: 引 docker-compose 服务栈 + React 前端异构;否决 LangSmith 云: 数据出境+付费+网络)。trace 表 schema 按 Langfuse 兼容子集设计,保留日后迁移 OSS 的退路。
2. **数据模型 — 两表 run→span 层级**(来自设计稿原型,Langfuse 字段命名习惯):

   ```sql
   trace_runs(run_id PK, session_id, run_type live_ask|live_resume|eval, mode,
              status ok|interrupted|error|degraded, query, final_answer,
              metrics JSONB, started_at, ended_at)
   trace_spans(id BIGSERIAL PK, run_id FK, span_type node|tool|llm|hitl,
              name, status, input JSONB, output JSONB,
              latency_ms, token_usage JSONB, started_at)
   ```

   span input/output 截断存储(输入 500 字/输出 2000 字封顶),原文可从 checkpoints 回捞。
3. **插桩复用 status 总线,不引 LangChain callback handler**: 节点已全部显式 async 函数且已有 `_publish_status` 挂点;方案是 `_REASONING_BUS` 同一 put 处双写(内存队列给 SSE,span 收集器攒给 DB),`_run_sse` 收尾统一落库(ask 与 resume 已共用此函数,天然覆盖两路)。
4. **评测框架自研 DeepSeek-as-judge,不引 RAGAS/DeepEval**(RAGAS 强绑 OpenAI 系 embedding,与 bge/中文语料错配;引框架带来传递依赖进稳定环境)。防漂移: 双盲判 + 每批 5 条人工锚定样本,judge 与锚偏差 >1 档整批判次作废重跑。
5. **Golden set 40 条双来源**: 30 条自动 —— Pinecone 694 块按 case_cause/year/chunk_index=0 分层抽样,DeepSeek 从 chunk_text 提炼"当事人视角问题",该案例自身即 hit ground truth;10 条人工 —— 完整咨询场景含期望法条(如民法典 1062/1087/1092)与预期 clarify 轮数。存 data/eval/*.jsonl(数据文件不进库,可重生成)。
6. **指标四层与首批目标线**: 检索 hit_rate@5 ≥0.70 / MRR@10 ≥0.50 / context precision(judge 0-2 分)≥1.5;流程 clarify ≤3 轮 / replan ≤0.3 / 中断率与工具错误率观测;答案 法条引用 ≥0.80 / 幻觉率 ≤0.10 / 人设 ≥0.80;HITL semantic_confirm ≥0.90;feedback 相关性观测基线。
7. **Eval runner**: `scripts/run_eval.py --suite retrieval|e2e|all --label <git_sha> --baseline <label>`;每 case 起 `run_type=eval` 运行,直调 graph 不走 HTTP(便于并发与 thread_id 隔离),结果落 eval_runs(dataset, label, metrics JSONB, cases JSONB)。
8. **监控 UI 挂前端 Vue Router `/monitor` 路由**,不做独立服务;复用 Inspira UI 组件。REST 契约: `GET /admin/traces`(列表过滤)、`GET /admin/traces/{run_id}`(含 span 瀑布)、`GET /admin/evals`(批次指标)。生产后续可加口令中间件。
9. **P0 前置修复**(评测的前提): PINECONE_NAMESPACE 环境变量参数化(默认 law_cases,.env 设 Law_test_namespace);`.env RETRIEVER_BACKEND=pinecone`;upsert_session 包 `_safe_*` 降级;checkpoint 断连自愈检查。
10. **eval 与 live 隔离**: eval thread_id 前缀 `eval-`,查询处过滤 run_type 使 eval 会话不进 /sessions 用户列表。
11. **DDL 幂等**: trace 表进 db.py ensure_tables(CREATE TABLE IF NOT EXISTS),与既有 5 张业务表同机制。
12. **阶段依赖**: P0→P2(检索评测前提),P1→P3(trace 前提),P2/P3 可并行,P4 收口门禁。

## Testing Decisions

- **好测试只测外部行为**: 指标计算是纯函数(hit_rate/MRR/聚合/截断逻辑)→ 直接断言数值,不测内部结构;trace 落库测试断言"跑一次图后表里出现对应 run/span 行",不测收集器内部实现。
- **测试接缝(seams)**:
  1. **最高接缝 — `scripts/run_eval.py` 进程级调用**(P2/P4): 对真实 graph + 真实 LLM 跑小型 suite,断言 eval_runs 出行、指标在合理区间。已有先例: scripts/gen_nb07.py 同为"真实服务+真实 LLM"模式。
  2. **REST 接缝 — `/admin/traces` 端点**(P3): HTTP 层断言列表/详情返回,复用 test_smoke 的 FastAPI TestClient 模式。
  3. **表接缝 — ensure_tables DDL**(P1): 幂等重复执行不报错,沿用现有 PG 集成测试模式(真实 PG,不可用时显式 SKIP,不 mock)。
- **不新增接缝**: 不为 span 收集器单独开测试入口 —— 它的行为通过 seam 1/2 的端到端结果间接验证(同一处代码已由 status 总线测试覆盖)。
- **评测自检**: golden 中 5 条锚定样本的 judge 输出与人工定标偏差监控,即 judge 漂移检测本身作为常驻测试资产。
- **存量不动**: 现有 15 个 pytest 不改动;新增测试独立文件。

## Out of Scope

- 多用户/权限体系(仅单管理页 + 后续口令)
- 在线实验(A/B 分流)
- 自动 prompt 优化(DSPy 类)
- 分布式追踪(单机单进程,无跨服务 OTLP)
- Langfuse OSS 迁移(仅保留 schema 兼容退路,不实施)
- 监控页实时推送(本期 REST 拉取,SSE 推送不做)

## Further Notes

- 评测旁路原则: trace/eval 任何失败不得阻塞业务;但业务本身失败显式报错,不做静默兜底(沿用仓库"有错误直接报"约定)。
- pg_stat n_live_tup 会显示陈旧值(实测 law_vector 显示 0 实有 1526 行),监控页计数以精确 count 为准。
- 评测 judge 用 DeepSeek,与被评 agent 主模型同源时注意双盲设计(judge 看不到 agent 的 prompt 版本标签)。
- 后续 prompts.py 变更的回归门禁(P4)建议接入提交流程而非 CI(本机开发为主,无远端 CI)。
