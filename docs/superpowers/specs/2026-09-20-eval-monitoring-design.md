# 法务 Agent 观测与评测系统(LangSmith 类)设计

- 日期: 2026-09-20
- 状态: 待评审
- 路径: Architectural(新子系统)
- 决策方式: 全自动推演(用户指定免问答),关键决策点在 §2 逐条给出取舍理由,评审时可推翻

## 1. 背景与现状(2026-09-20 实测)

### 1.1 数据资产

| 资产 | 位置 | 状态 |
|---|---|---|
| 案例语料 | Pinecone `pinecone-test-lawapp` / ns `Law_test_namespace` | **694 块**离婚纠纷判决书分块,元数据 `{case_cause, case_number, chunk_index, chunk_text, year}`,向量 1024 维(bge-large-zh-v1.5) |
| 法条库 | 本地 PG(15432) `law_vector` 表 | 已灌(fetch_laws 实测命中 5-8 条) |
| 案例库(本地) | 同库 `law_cases` 表 | **空** — `ingest_cases_pgvector.py` 未跑 |
| 会话/审计 | `sessions` / `audit`(JSONB payload) / `feedback`(rating+answer_snapshot) | 表结构就绪,数据随使用累积 |

### 1.2 关键缺陷(检索链路断裂,P0 必修)

1. **命名空间错配**: 代码全线默认 `ns="law_cases"`(`rag_tools.py` / `RAG_service/base.py` / 两个 retriever),但 Pinecone 实际数据在 `Law_test_namespace`。
2. **后端错指**: `.env RETRIEVER_BACKEND=pgvector` → 指向空的 `law_cases` 表。
   → 两者叠加 = E2E 实测 `retrieve_legal_knowledge | 未检索到相关案例`。**当前 agent 是"瘸腿"运行,只靠法条+联网搜索撑结论。**
3. **PG 断连未降级**: Docker Desktop 掉线时 `/sessions` 直接 500(`upsert_session` 未包 `_safe_*`);checkpoint 后端已选 postgres 后断连无自愈。

### 1.3 已有可复用底座(这是选自建路线的底气)

- `audit` 表: `(session_id, event_type, payload JSONB)` — 事件总线已落库
- `agent_flow.log` + `system.log`: JSON 行式全流程日志(节点进出/工具耗时/HITL 决策)
- 全节点状态插桩 + `_REASONING_BUS`(Batch B 刚完成): CoT/状态/计划流基础设施
- `build_tool_usage`: 工具级结果 JSON 记录
- `prompts_record` / `final_prompts`: LLM 输入输出快照随响应下发
- `evaluate_case_relevance` + CRAG 三档阈值(0.5/0.2/3): 检索质量门控已有信号
- `semantic_confirm`: HITL 语义判定可回放
- `scripts/gen_nb07.py`: 真实服务+真实 LLM 的评测笔记本生成器模式
- 前端 Vue3 + Inspira UI 组件库(监控页可直接复用)

## 2. 关键决策(全自动推演,含被否方案)

### D1 自建 vs 开源 vs 云服务 → **自建轻量(复用 PG)**

| 方案 | 否决/采纳理由 |
|---|---|
| **A. 自建**(采纳) | 数据全在 PG 单一存储;audit/日志/工具记录底座已 60% 就绪;法律咨询数据不出境;零新基础设施(今天 Docker Desktop 掉线正好证明少一个外部依赖的价值) |
| B. Langfuse OSS 自托管 | UI/tracing 现成,但引入 docker-compose 服务栈(本轮 Docker 不稳)+ 数据模型双向适配 + 前端异构(React vs Vue)。若自建 2 个月后仍不敷用,可作 B 计划迁移(trace 表 schema 按 Langfuse 兼容子集设计,保留退路) |
| C. LangSmith 云 | 最快,但数据出境 + 付费 + 国内网络不稳,法律场景直接否决 |

### D2 评测框架 → **DeepSeek-as-judge + 规则指标,不引 RAGAS/DeepEval**

理由: 指标就 4-5 个,judge prompt 自己写更可控;RAGAS 强绑 OpenAI 系 embedding 语义,与 bge/中文案例库错配;引框架 = 引一坨传递依赖进已能跑 15 测试的稳定环境。**风险**: judge 本身会漂移 → 用**双盲判 + 锚定样例**(每批评测带 5 条人工定标样本,judge 输出与锚偏差>1 档即整批判次作废重跑)。

### D3 Trace 数据模型 → **trace_runs + trace_spans 两表,Langfuse 兼容子集**

```sql
CREATE TABLE IF NOT EXISTS trace_runs (
    run_id      TEXT PRIMARY KEY,          -- {session_id}:{turn_seq} 或 eval:{dataset_id}:{case_id}
    session_id  TEXT,
    run_type    TEXT NOT NULL,             -- live_ask | live_resume | eval
    mode        TEXT,                      -- attorney | assistant
    status      TEXT NOT NULL,             -- ok | interrupted | error | degraded
    query       TEXT,
    final_answer TEXT,
    metrics     JSONB DEFAULT '{}',        -- 聚合指标(轮数/工具数/时延/token)
    started_at  TIMESTAMPTZ, ended_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS trace_spans (
    id          BIGSERIAL PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES trace_runs,
    span_type   TEXT NOT NULL,            -- node | tool | llm | hitl
    name        TEXT NOT NULL,             -- planner / retrieve_legal_knowledge / semantic_confirm...
    status      TEXT,                      -- ok | error | interrupted
    input       JSONB, output JSONB,       -- 截断存储(输入 500 字/输出 2000 字封顶)
    latency_ms  INT,
    token_usage JSONB,                     -- {prompt, completion} 有则记
    started_at  TIMESTAMPTZ
);
```

采纳 Langfuse 的 run→span 层级与字段命名习惯(B 计划迁移退路),不采纳其 OTLP 协议栈。

### D4 插桩方式 → **复用 Batch B 的 status 总线模式,不引 callback handler**

LangChain callback handler 看似标准,但本项目图节点已全部显式 async 函数且已有 `_publish_status` 插桩点 — 在节点出口写 span 与发 status 是同一处代码,两套机制只会漂移。方案: `_REASONING_BUS` 旁挂一条持久通道(同一 put 处双写: 内存队列给 SSE,span 收集器给 DB),`_run_sse` 收尾时统一落库。

### D5 监控 UI → **前端加 /monitor 路由(不做独立服务)**

管理页非用户侧功能,但不值得独立进程。Vue Router 加路由 + 复用 Inspira 组件(表格/卡片),REST: `GET /admin/traces?...`、`GET /admin/traces/{run_id}`(含 spans 瀑布)、`GET /admin/evals`。生产可后续用简单口令中间件保护(法律数据敏感)。

## 3. 评测指标体系(核心交付)

### 3.1 数据集: golden set 构造

- **规模**: 首批 40 条(自动生成 30 + 人工定标 10)
- **来源 1(检索质量, 30 条)**: 从 Pinecone 694 块中分层抽样(按 case_cause/year/chunk_index=0),每案例自动生成 query(DeepSeek 从 chunk_text 提炼"当事人视角问题"),期望命中 = 该案例自身(hit_rate 的 ground truth)→ **同时解决 ns 错配的回归防护**
- **来源 2(端到端, 10 条)**: 人工编写完整咨询场景(含期望要素、期望引用法条如民法典 1062/1087/1092、期望结论要点、预期 clarify 轮数)
- 存储: `data/eval/golden_retrieval.jsonl` + `data/eval/golden_e2e.jsonl`(数据文件不进库,脚本可重生成)

### 3.2 指标分层

| 层 | 指标 | 判定方式 | 目标线(首批) |
|---|---|---|---|
| 检索 | hit_rate@5 / MRR@10 | 规则(golden 案例是否在 top-k) | ≥0.70 / ≥0.50 |
| 检索 | context precision(检索块与问题相关性) | LLM judge 0-2 分 | ≥1.5 |
| 流程 | clarify 轮数 / replan 率 / 中断率 / 工具错误率 | 规则(trace_spans 聚合) | ≤3 轮 / ≤0.3 |
| 流程 | P50/P95 时延, token 成本 | 规则(usage 聚合) | 观测基线 |
| 答案 | 法条引用正确性 | LLM judge 对照 law_vector 原文 | ≥0.80 |
| 答案 | 幻觉率(结论无检索支撑) | LLM judge 双盲 + 锚定 | ≤0.10 |
| 答案 | 人设一致性(kim/saul 风格) | LLM judge 对照人设卡 | ≥0.80 |
| 用户 | feedback 评分分布与指标相关性 | 规则(feedback 表 join trace) | 观测基线 |
| HITL | semantic_confirm 准确率 | 回放 20 条标注样本 | ≥0.90 |

### 3.3 Eval runner

`scripts/run_eval.py`:
- `--suite retrieval|e2e|all --label <git_sha 或标签>`
- 每条 case: 起 eval run(`run_type=eval`,直接调 graph 不走 HTTP,便于并发与隔离 thread_id)+ judge 评分 + 指标聚合
- 落库 `eval_runs(dataset, label, metrics JSONB, cases JSONB)` + 控制台摘要表
- **对比模式**: `--baseline <label>` 输出两批指标 diff → prompts.py 改动的回归门禁

## 4. 发展路径(阶段排期)

```
P0 检索修复(前置, 0.5 天)
 ├─ PINECONE_NAMESPACE 环境变量参数化(默认 law_cases, .env 设 Law_test_namespace)
 ├─ .env RETRIEVER_BACKEND=pinecone; 验证 retrieve_legal_knowledge 实测命中
 └─ upsert_session 包 _safe_* 降级(PG 掉线不再 500); checkpoint 断连自愈检查
P1 Trace 落库(1-2 天)
 ├─ trace_runs/trace_spans 表(db.py 幂等 DDL)
 ├─ span 收集器 + 节点/工具/LLM/HITL 插桩(复用 status 总线挂点)
 └─ _run_sse 收尾统一落库(ask 与 resume 两路共用,已是同函数)
P2 评测流水线(2-3 天)
 ├─ golden set 生成脚本( Pinecone 抽样→query 生成)+ 人工定标 10 条
 ├─ scripts/run_eval.py(judge 双盲+锚定)+ eval_runs 表
 └─ 首批基线跑出并存档(label=baseline-2026-09)
P3 监控 UI(2-3 天)
 ├─ GET /admin/traces(+/{run_id}) /admin/evals 端点
 └─ 前端 /monitor 路由: run 列表→span 瀑布视图→指标看板→feedback 对齐
P4 回归门禁(1 天)
 └─ eval 接入提交流程: prompts.py 变更必跑 e2e suite 对比 baseline, hit_rate/幻觉率退化即红
```

依赖关系: P0 是 P2 检索评测的前提;P1 是 P3 的前提;P2/P3 可并行。

## 5. 错误处理与边界

- judge 调用失败/超时: 单 case 记 `judge_error`,整批完成率 <80% 即批失败(不做静默兜底,沿用"有错误直接报"约定)
- trace 写库失败: 不阻塞主流程(评测是旁路),记 system.log ERROR + 计数器
- eval run 与 live run 隔离: 独立 thread_id 前缀 `eval-`,session 不入 `/sessions` 列表(查询过滤 run_type)
- 截断原则: span input/output 超限截断存摘要,原文可从 checkpoints 回捞

## 6. 测试策略

- 单元: 指标计算纯函数(hit_rate/MRR/聚合)pytest;span 截断逻辑
- 集成: trace 落库走真实 PG(沿用 test_smoke 模式, PG 不可用显式 SKIP)
- 评测自检: golden 中 5 条锚定样本的 judge 输出与人工定标偏差监控(judge 漂移检测)

## 7. 非目标(本期不做)

- 多用户/权限体系(单管理页+口令)
- 在线实验(A/B 分流)
- 自动 prompt 优化(DSPy 类)
- 分布式追踪(单机单进程,无跨服务)
