# Agent 阶段监控平台(拉链表 + 甘特图 + 评测算分) — 设计文档

- 日期: 2026-10-08
- 状态: 定稿(brainstorming 全轮次决策经用户确认)
- 前置关系: 完成 [2026-09-20-eval-monitoring-spec.md](2026-09-20-eval-monitoring-spec.md) 的 P2(评测)与 P3(监控 UI)未落地部分, 并以拉链表/甘特图/综合评分扩展
- 未提交背景: 会话开始时 `db.py`/`tracing.py` 仅有自动格式化 diff, 无功能冲突

## Problem Statement

Agent 全链路已跑通(咨询/HITL/docx/对话日志), trace 层(P1)已落库, 但:

1. **阶段状态不可实时观测**: `trace_spans` 在 run 结束才批量落库(`flush_run`), 进行中运行完全黑盒, 事后也只能看 span 列表拼时间线。
2. **无监控页面**: 2026-09-20 spec 的 P3(`/monitor` 单页)未实现, 前端无路由, 无表格库, 排查一次异常会话需手写 SQL。
3. **无评测能力**: P2 未实现——无 golden set、无 hit_rate/MRR 指标、无 eval_runs 表, 检索质量断裂(如命名空间错配复现)无量化暴露; 准确率/召回率/F1 无计算能力。
4. **无运行评分**: 完成时间/token/工具调用数已在 metrics 聚合但无综合分, 无法一眼分好坏 run。

用户需要: 数据库拉链表记录每阶段状态(实时+历史)、前端监控页(甘特图)、从原前端页面可跳转、检索分数保留展示、执行次数记录、综合指标总分, 并为 LLM-as-judge 预留扩展位。

## 已确认决策(brainstorming 访谈轮)

| # | 决策 | 选择 |
|---|------|------|
| D1 | 阶段粒度 | **图节点级**(15 节点逐一记拉链行, 复用现有 @traced 埋点, 改动最小) |
| D2 | 实时性 | **实时+历史**(节点开始开行、结束闭行落库; 事后可查全部历史) |
| D3 | 与现有 trace 表关系 | **新建独立拉链表** `stage_chain`; trace_runs/trace_spans 不动, 零回归, Langfuse 兼容不破 |
| D4 | 前端跳转 | **vue-router 新路由** `/monitor`, 主页头部入口链接, URL 直达可刷新 |
| D5 | 拉链行写入路径 | **扩展 @traced 双写**(wrapper 内开行/闭行, fire-and-forget, 失败仅告警, 不碰图代码) |
| D6 | 时间线形式 | **甘特图**(共享时间轴, 节点行, 重复执行叠行), CSS 自绘高度自定义, 借鉴成熟甘特库交互(hover tooltip/时间网格/缩放), 配合页面主题色; runs 列表行内 mini 瀑布条 |
| D7 | 进行中 run 的内容明细 | **可接受不实时**: 甘特/状态实时(拉链), 阶段全文明细(工具结果/state)来自 trace_spans, run 结束后可见; 甘特主要定位是事后监控 agent 发展 |
| D8 | 检索分数 | hybrid_score 已存 trace_spans.output, 补拉链行 detail 轻量摘要, 监控页展示 |
| D9 | 执行次数 | stage_chain.seq 记录重跑序号, metrics 扩展总执行数/触顶标志 |
| D10 | 综合评分 | 纯函数加权合成 0-100, 权重 config 可调, flush 时算好存 metrics |
| D11 | LLM-as-judge | **预留不实现**: JSONB metrics 零迁移扩展, 综合分函数留 judge 插槽, 后续路径记录在 Out of Scope |

## Solution

### 一、数据层(db.py)

新增拉链表与评测表, 进 `ensure_tables` DDL(CREATE TABLE IF NOT EXISTS, 幂等):

```sql
-- 阶段拉链表(SCD2 语义): 开行 INSERT / 闭行 UPDATE
CREATE TABLE IF NOT EXISTS stage_chain (
    id          BIGSERIAL PRIMARY KEY,
    run_id      TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    node_name   TEXT NOT NULL,          -- 15 图节点之一
    seq         INT NOT NULL,           -- 该 run 内此节点第几次执行(replan 回环重跑递增)
    status      TEXT NOT NULL DEFAULT 'running',
                -- running | ok | error | interrupted | cancelled
    started_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at    TIMESTAMPTZ,            -- NULL = 进行中(拉链开口)
    is_current  BOOLEAN NOT NULL DEFAULT TRUE,
    latency_ms  INT,
    detail      JSONB DEFAULT '{}'::jsonb
                -- 轻量摘要: 错误 repr / interrupt 类型 / rag 摘要
                -- {rag_top_score, rag_count, rag_verdict, exception}
);
CREATE UNIQUE INDEX idx_stage_chain_slot ON stage_chain(run_id, node_name, seq);
CREATE INDEX idx_stage_chain_open ON stage_chain(ended_at) WHERE ended_at IS NULL;
CREATE INDEX idx_stage_chain_session ON stage_chain(session_id, started_at);

-- 评测批次表(P2, 2026-09-20 spec 决策 2 原样)
CREATE TABLE IF NOT EXISTS eval_runs (
    id          BIGSERIAL PRIMARY KEY,
    dataset     TEXT NOT NULL,          -- retrieval | e2e
    label       TEXT NOT NULL,          -- git sha 或自定义
    metrics     JSONB DEFAULT '{}',
    cases       JSONB DEFAULT '[]',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
```

拉链语义: **开行** = INSERT(status=running, ended_at NULL, is_current TRUE); **闭行** = UPDATE 补 ended_at/status/latency_ms/detail, 翻 is_current=FALSE。历史 = 闭行全量; 活动断面 = `ended_at IS NULL`。闭行定位不靠 id, 用 `(run_id, node_name, seq)` 唯一索引。

`db.py` 新增写入助手 `open_stage()` / `close_stage()`, 失败 `logger.warning` 放行——观测旁路语义, 与 `record_audit`/`flush_run` 一致。

### 二、埋点层(tracing.py)

不碰图代码(LangGraph_lawApp.py 零改动), 只扩展 `@traced` 装饰器:

- **进入时**: `span_type == "node"` 且 RunContext 存在 → fire-and-forget `open_stage(run_id, session_id, node_name, seq)`。seq 由 RunContext 新增 per-node 计数器(`dict[str, int]`)自增——回环重跑各拿新号
- **退出时**(ok/error/interrupted/cancelled 全部分支, 含 M5 BaseException 分支) → `close_stage(run_id, node_name, seq, status, latency_ms, detail)`。detail 仅存轻量摘要: 错误分支存 `{"exception": repr(e)[:300]}`; 检索节点产出含 rag_documents 时存 rag 摘要
- **fire-and-forget 实现**: async wrapper 内 `asyncio.create_task` + 任务异常吞并 log; sync wrapper 经 `asyncio.get_running_loop()` 拿 loop 后同样 create_task, 无 running loop(单测直调节点)时静默跳过不报错
- LLM/tool span **不写拉链表**(只 node)——拉链表管阶段状态, 明细归 trace_spans
- 写入顺序保证: 开行先于闭行(同一 task 链内先后 create_task, PG 单连接池串行提交; 若极小概率乱序, 闭行 UPDATE 无匹配行时由 `ensure_stage_closed` 兜底忽略, 不报错)

### 三、指标层

**1. metrics 扩展(flush_run 处)**: `trace_runs.metrics` JSONB 新增键:
- `node_exec_total`(节点执行总次数, 含重跑)
- `replan_rounds`(replanner 触发次数)
- `clarify_rounds`(已有)
- `limit_hit`(布尔: 触达 recursion_limit=60 / max_rounds=10 / max_clarify_rounds=5 任一上限)
- `composite_score`(见下)

**2. 综合评分 `lib/score.py`**(纯函数, 无 IO):
- `composite_score(metrics: dict, weights: dict) -> int` (0-100)
- 分量: 检索质量(rag_top_score 与三档分布, 无检索则不计入权重)/ 时延(相对基线, 首版基线取已跑 runs 的 P50)/ token 成本(相对基线)/ 工具错误率 / HITL 中断+降级次数 / replan 轮数
- 各分量归一化 0-1 后加权; 权重字典进 config.py(`score_weights`)可调
- **judge 插槽**(D11): 签名留 `judge_score: float | None = None` 参数, None 时该分量权重重分配, 不影响总分构成——后续 LLM-as-judge 接入零重构

**3. 检索评测指标 `lib/eval_metrics.py`**(纯函数):
- `hit_rate_at_k(results, k=5)` / `mrr_at_k(results, k=10)` / `precision_at_k` / `recall_at_k` / `f1_at_k`
- **P/R/F1 说明**: golden set ground truth 为每用例单条目标案例, recall@k ∈ {0,1}、precision@k 退化为近似 hit_rate, F1 数值意义有限; 指标一并实现并输出, 待 golden set 扩展为多相关案例标注(按 case_cause 同类)后自动有意义——此为已知局限, 记录不阻塞

### 四、评测模块 P2 落地

- `scripts/gen_golden_set.py`: Pinecone 案例块按 case_cause/year/chunk_index=0 分层抽样, LLM 从 chunk_text 提炼当事人视角问题, 该案例即 hit ground truth; 产出 `data/eval/retrieval.jsonl`(30 条, 数据文件不进 git 可重生成)
- `scripts/run_eval.py --suite retrieval --label <git_sha> [--baseline <label>]`: 每用例 `run_type=eval`、`eval-` 前缀 thread_id 直调 graph(不走 HTTP), 聚合指标落 `eval_runs`; `--baseline` 输出两批 diff; eval 流量不进 /sessions 用户列表(查询处过滤 run_type, 机制已有)
- 首批纯观测不设目标线(对齐 2026-09-20 spec 决策 13)

### 五、API 层(api.py + model.py)

| 端点 | 作用 |
|---|---|
| `GET /monitor/overview` | 近 24h: runs 各状态计数、进行中阶段数、触顶 run 计数、composite_score 分布、节点失败排行 |
| `GET /monitor/runs?limit=&status=&session_id=` | runs 列表: trace_runs join stage_chain 聚合(阶段进度 n/total、进行中标记、node_exec_total、limit_hit) + rag 摘要列 |
| `GET /monitor/runs/{run_id}/stages` | 单 run 全景: 拉链行(甘特数据源: started_at/ended_at/status/seq/detail) + 关联 trace_spans 明细(node/tool/llm 全文, 按 node_name 对齐) |
| `GET /monitor/evals?dataset=&limit=` | 评测批次列表(指标 + baseline 对比字段) |

Pydantic 模型 `MonitorOverview / MonitorRunItem / MonitorStageDetail / MonitorEval` 进 model.py。端点读取失败按现有降级风格处理(PG 掉线不 500)。

### 六、前端(vue-router + MonitorView)

**路由**: 新增 `vue-router@4` 依赖。现 App.vue 全部内容原样搬至 `views/ChatPage.vue`; App.vue 改为 `<router-view>` 壳; `src/router.js` 定义 `/`(ChatPage) + `/monitor`(MonitorView)。

**入口跳转**: 聊天页头部加"监控"入口链接 → `/monitor`; MonitorView 顶部"返回咨询"链接。

**MonitorView.vue 页面结构**(自上而下堆叠联动):
1. 总览卡片区(overview: 状态计数/进行中/触顶告警/分数分布/失败节点排行)
2. runs 列表: 时间/session/mode/状态/阶段进度(mini 瀑布条, 各阶段耗时占比堆叠着色)/node_exec_total/limit_hit/composite_score; 行点击进入详情
3. **甘特图**(`components/GanttTimeline.vue`): 共享时间轴, 节点行×seq 叠行; 条形 started_at→ended_at, running 条延伸至"现在"+呼吸动画; 着色 ok 绿/error 红/interrupted 黄/cancelled 灰, 与页面主题色一致; CSS 自绘(借鉴成熟甘特库的 hover tooltip/时间网格/缩放交互), 不引图表库; 条上 tooltip: 节点名/第N次/耗时/status
4. **阶段详情抽屉**(甘特条点击): 该节点 input/output/state 全文折叠面板——工具结果全文(rag_documents 列表: case_number + chunk 摘要 + **hybrid_score 分数条**, 三档配色 correct≥0.5 绿/ambiguous≥0.2 黄/incorrect 红)、LLM 输入输出、token
5. 指标看板卡(evals 批次: hit_rate@5/MRR@10/P@5/R@5/F1@5 + baseline diff)

**实时刷新**: 3s `setInterval` 轮询, 检测到无 running 行则停轮(只查一次收尾); 组件卸载清除定时器; 不动现有 SSE 通道。

**前端文件**: `router.js`(新)/`views/ChatPage.vue`(新, 原样搬)/`views/MonitorView.vue`(新)/`components/GanttTimeline.vue`(新)/`components/StageDetailDrawer.vue`(新)/`App.vue`(改壳)/`main.js`(挂路由)/`api.js`(monitor 封装)/`package.json`(vue-router)。

### 七、测试

- 新 `tests/test_stage_chain.py`: DDL 幂等/开闭行/拉链语义(闭行后 ended_at 非空 is_current=FALSE)/seq 回环重跑/同步 wrapper 无 loop 旁路/@traced 双写集成(真实 PG 跑一图, 断言拉链行齐)
- 新 `tests/test_monitor_api.py`: 4 端点(列表/详情/overview/evals), TestClient 模式对齐 test_smoke
- 新 `tests/test_score.py` + `test_eval_metrics.py`: 纯函数直接断言数值(含 judge 插槽 None 行为/P-R-F1 退化路径)
- 前端: `vite build` 零错误(项目现状无前端单测, 对齐)
- 回归: 全量 pytest 157 用例零失败

## Testing Decisions(对齐 2026-09-20 spec 风格)

- 好测试只测外部行为: 拉链测试断言"跑一次图后表里出现对应闭行"; 指标纯函数断言数值; 装饰器测试断言行为(异常透传不吞/无 run 上下文静默跳过)
- 测试接缝: ①表接缝 ensure_tables 幂等(真实 PG) ②REST 接缝 TestClient ③纯函数零依赖
- 存量不动: 现有 pytest 不改动, 新增测试独立文件

## Out of Scope

- **LLM-as-judge 实现**(D11 预留: metrics JSONB 加 judge_score 零迁移; composite_score 已留参数插槽; 后续路径 = e2e golden set 5 条 → judge 打分 → eval_runs.metrics)
- **spans 逐节点实时落库**(D7: 进行中 run 内容明细不实时, 甘特状态实时足够; 改动现有旁路语义回归面大)
- golden set 多相关案例标注扩展(本期单条 ground truth, P/R/F1 已知退化记录在案)
- 监控页访问控制(本地本机)
- e2e 评测 suite(本期只落 retrieval; e2e golden set 5 条待 judge 模式一并)
- trace 数据清理策略(无限保留)
- vxe-table 引入(runs 列表自绘简化表格即可, 数据量单机可控)

## Further Notes

- 观测旁路原则贯穿: 拉链/评测/评分任何失败不得阻塞业务, 失败仅 log 放行
- stage_chain 与 trace_spans 职责分工: 拉链=阶段状态(实时), spans=内容明细(事后全文); 监控详情页二者按 (run_id, node_name) 对齐 join
- seq 计数器在 RunContext(内存), 进程重启丢历史不影响——已落库行不依赖内存
- recursion_limit=60 / max_rounds=10 / max_clarify_rounds=5 / error_streak_threshold=2 为现有 config 值, limit_hit 判定复用, 不新设上限
- 甘特图为事后监控主视图(D7), 实时态仅补充进行中 run 的状态条

## 执行记录(2026-10-08, 监控全量落地)

**提交链**:
- 118054a D: spec 存档(D1-D11)
- 58b6a1a P: 9 任务 TDD 实施计划
- c71dec2 Task1: stage_chain/eval_runs DDL + open/close_stage/insert_eval_run 助手
- 90c3964 Task2+3: @traced 拉链双写 + STAGE_ZIPPER 门控 + get_pool 跨 loop 锁重建 + score.py 综合评分(6 分量/缺数据重分配/judge 插槽)
- c8dc4d6 Task4: eval_metrics 纯函数(hit_rate/MRR/P/R/F1@k)
- 3f4490f Task5: gen_golden_set(Pinecone 分层抽样 + DeepSeek 提炼问题) + run_eval(retrieval suite + --baseline diff)
- e22e85e Task6: /monitor 4 端点 + 6 响应模型(PG 掉线降级空态)
- (Task7) vue-router@4 接入: App 改 router-view 壳/ChatPage 搬迁/header 监控入口
- (Task8) MonitorView 实装: 总览 4 卡/评测批次表/runs 列表/甘特 CSS 自绘/阶段抽屉 rag 分数条三档/3s 智能轮询
- (Task9 修复) close_stage 有界重试 + 全量回归 183 PASS/0 FAIL

**门禁**: 全量 pytest 183 PASS / 0 FAIL / 0 SKIP(真实 PG 15432); vite build 零错误。

**评测基线**: golden set 30 条(案由分层), label=baseline-001:
hit_rate@5=0.667 / MRR@5=0.550 / P@5=0.367 / R@5=0.226 / F1@5=0.279
(R 偏低属预期: relevant_ids=同案例全部 chunk, top-5 只能覆盖部分)。

**偏差存档(8 项)**:
1. gen_golden_set 改从 Pinecone 命名空间采样(计划 PG law_cases — 实测该表 0 行, 语料全在 Pinecone, 与 2026-09-20 spec 对齐)
2. run_eval 直接绑 PineconeRetriever 实例(计划 get_retriever() — 后者读 os.environ, 脚本进程无 .env 注入会错落 pgvector 后端, id 空间不同致全 0 分)
3. close_stage 闭行有界重试 6 次×50ms(计划/Task1 为静默忽略 0 行 — 全量回归暴露真实竞态: 开行 INSERT 与闭行 UPDATE 并发 fire, UPDATE 先到 0 行跳过则行永远 running)
4. tests PG 门控用仓库 _pg_ok() 独立连接探测(计划 @PG skipif 标记 — 对齐存量测试风格)
5. TestClient 无 /api 前缀(计划注释已预告需对齐 test_smoke, 确认无前缀)
6. runs 列表未做 mini 瀑布条(计划 Task8 简化为综合分/触顶列 — 甘特详情已承载耗时可视化, 数据量单机可控)
7. retrieve 指标统一 k=5(spec §五曾提 MRR@10 — 计划已定 k=5, evaluate 单 k 聚合)
8. score_distribution 用 width_bucket 三桶 [0,34)/[34,67)/[67,100](计划原文如此, 前端总览卡暂未消费该字段)

**遗留(Out of Scope 重申)**: LLM-as-judge 实现(D11 插槽已留)/spans 实时落库/e2e 评测 suite/监控页访问控制。
