# lawApp_LangGraph — 法律智能咨询 Agent

<div align="center">

基于 **LangGraph Plan & Execute** 架构的婚姻家庭法 AI 咨询系统，集成混合检索增强生成 (CRAG)、人机协同审批 (HITL)、长期记忆、Word 法律文书生成、对话日志审计与 Vue3 流式前端。

[![Python](https://img.shields.io/badge/Python-3.11-3776AB?style=flat&logo=python)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.136-009688?style=flat&logo=fastapi)](https://fastapi.tiangolo.com/)
[![LangGraph](https://img.shields.io/badge/LangGraph-1.0-1C3C3C?style=flat&logo=langchain)](https://langchain-ai.github.io/langgraph/)
[![Pinecone](https://img.shields.io/badge/Pinecone-Serverless-1C17FF?style=flat)](https://www.pinecone.io/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-15-4169E1?style=flat&logo=postgresql)](https://www.postgresql.org/)
[![License](https://img.shields.io/badge/License-MIT-yellow?style=flat)](LICENSE)

</div>

---

## 目录

- [架构概览](#架构概览)
- [核心亮点](#核心亮点)
- [Agent 工作流](#agent-工作流)
- [人机协同 HITL](#人机协同-hitl)
- [检索系统](#检索系统)
- [律师助理文书生成](#律师助理文书生成)
- [工具链](#工具链)
- [API 接口](#api-接口)
- [数据模型](#数据模型)
- [日志系统](#日志系统)
- [对话日志与追踪](#对话日志与追踪)
- [快速开始](#快速开始)
- [项目结构](#项目结构)
- [技术栈](#技术栈)

---

## 架构概览

系统采用 **Plan & Execute** 范式：由强模型制定执行计划，轻量模型逐步执行，在保证回答质量的同时大幅降低推理成本。

```text
用户请求 → FastAPI → LangGraph StateGraph
                        │
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼
    planner         executor        replan_check
  (DeepSeek Pro)  (DeepSeek Flash)  (DeepSeek Flash)
        │               │               │
        │   ┌───────────┘         ┌─────┴─────┐
        │   ▼                     ▼           ▼
        │  工具调用            finalize    replanner
        │  ├─ Pinecone 混合检索     │        (DeepSeek Pro)
        │  ├─ pgvector 法条检索     │           │
        │  ├─ SerpAPI 联网搜索      │           ▼
        │  ├─ PostgreSQL 记忆       │       executor
        │  ├─ PDF 报告生成          │
        │  └─ Word 文书生成         │
        │   │
        │   ▼ interrupt() ──→ 前端 HITL 卡片 ──→ /ask/resume 续流
        └───────────────────────────┘
```

**双 LLM 成本优化**：规划/重规划使用 DeepSeek Pro（强推理），执行/质量门控使用 DeepSeek Flash（低成本低延迟），单次查询可节省 **40-60% token 消耗**。

---

## 核心亮点

### 架构设计

| 亮点 | 说明 |
|------|------|
| **Plan & Execute 柔性管线** | Agent 根据问题复杂度自主决策——闲聊跳过执行直接回答，法律问题走完整 CRAG 管线 |
| **人机协同 (HITL) 审批** | 7 类中断-恢复交互（问诊补全/中途澄清/高风险/降级/预算超限/PDF 报告/Word 文书确认），律师决策后基于 PostgreSQL checkpoint 无损续流 |
| **三级优雅降级** | JSON 解析失败 → 默认计划兜底；LLM 调用失败 → 参数映射兜底；语义判断失败 → 规则兜底；扩展：MCP 掉线降级、文书模板缺失整链回退纯咨询 |
| **质量闭环** | 检索 → 评估(三档) → 不足则联网补充 → 重规划 → 再执行，形成自纠正反馈环 |
| **双模式 + 律师助理** | 代理律师咨询模式（留案例+法条双检索）与律师助理模式（留法条、去类案、末步文书生成）共用一套图 |
| **全链路类型安全** | Pydantic v2 覆盖工具返回值 → 图状态 → API 响应的完整数据流 |

### 检索增强

| 亮点 | 说明 |
|------|------|
| **混合检索** | 密集语义匹配 (BGE) + 稀疏关键词匹配 (BM25)，可调 alpha 权重兼顾"意思相近"与"关键词命中" |
| **CrossEncoder 重排序** | BGE-reranker-large 全注意力交互打分，非简单向量距离，Top-N 精度大幅提升 |
| **CRAG 三档评估** | 将文档分为 correct / ambiguous / incorrect 三档，Agent 根据评估结论自主决定是否联网补充 |

### 工程实践

| 亮点 | 说明 |
|------|------|
| **配置中心** | pydantic-settings 单一入口（`config.py`），进程 env > `.env` > 默认值同名覆盖，零散 getenv 全部收拢 |
| **懒加载单例** | RAG 服务、嵌入模型、LLM、数据库连接全部延迟初始化，`threading.Lock` 双检防护多线程冷启动竞态 |
| **5 类结构化日志** | 通过 contextvars 实现 session_id 全链路透传；文件日志 JSON 行输出，结构化字段独立成键，可直接供日志采集消费 |
| **SSE 流式推送** | 实时推送推理 token、计划进度、工具调用、interrupt、文书生成事件，前端零刷新全流程可视 |
| **对话日志审计** | 全流程事件流落库（问答/澄清/确认决策/终答/文书），聚合 API 按轮次时间线还原，支持服务端中断后前端无损恢复 |
| **LLM 追踪** | `traced` 装饰器包裹全节点/工具/LLM 调用，trace run/span 落 PostgreSQL，兼容 LangSmith 观测 |
| **测试背书** | 15 个测试文件、157 条用例（真实 PostgreSQL 端到端 + 图内流程 + 安全边界），回归全绿 |
| **并发安全** | 基于 LangGraph PostgreSQL 检查点实现会话级隔离与中断恢复 |

---

## Agent 工作流

`LangGraph StateGraph` 包含 **14 个节点**，覆盖问诊、风险门控、CRAG 执行环与人机协同中断：

### 节点

| 节点 | 模型 | 职责 |
|------|------|------|
| `ingest` | — | 会话初始化、轮次状态重置、历史记忆装载 |
| `risk_gate` | Flash | 高风险咨询识别，触发 interrupt 请求律师确认 |
| `chitchat` | Flash | 闲聊短路，直接回答不过管线 |
| `ask_element` | Flash | 案情要素问诊补全（可出 interrupt 选项题） |
| `planner` | DeepSeek Pro (t=0.4, 4096 tokens) | 分析用户问题，输出 JSON 推理链 + 分步计划 |
| `executor` | DeepSeek Flash (t=0.25, 2048 tokens) | 按步骤调用工具，支持 LLM Function Calling 与直接参数映射降级 |
| `tools` | — | ToolNode 统一执行工具调用并回写状态 |
| `merge` | — | 解析工具输出，_STATE_KEYS 声明的键合并回图状态 |
| `replan_check` | DeepSeek Flash | 评估执行质量（案例数、评估结果、错误），判定是否需要重规划 |
| `mid_clarify` | Flash | 执行中途发现信息缺口时向律师发起澄清（interrupt） |
| `hitl_degrade` | Flash | 多轮澄清仍不足时向律师确认降级执行（interrupt） |
| `hitl_budget` | Flash | 规划步数超预算时向律师确认放行（interrupt） |
| `replanner` | DeepSeek Pro | 基于已执行步骤生成补充计划（不重复，最多 10 轮） |
| `finalize` | — | 组装最终答案（含引用），已有答案直接用，否则 LLM 兜底生成 |

### 路由逻辑

```text
START → ingest → risk_gate ──[风险确认]──→ interrupt → resume
                      │
                      └─→ chitchat ──[闲聊]──→ END
                          │
                          └─→ ask_element ─(interrupt)─→ planner ──[plan 为空]──→ finalize → END
                                          │
                                          └──[有步骤]──→ executor ─[tool_calls]→ tools → merge ─┐
                                                          │    └─[interrupt: pdf/docx/降级/预算]─┤
                                                          ▼                                    │
                                                  [有剩余步骤]→ executor (循环)               │
                                                          │                                    │
                                                          ▼                                    │
                                                      replan_check ─[通过]→ mid_clarify → finalize → END
                                                          │
                                                          └─[未通过]→ replanner → executor
```

### 多轮对话示例

```text
用户: "离婚后彩礼能要回来吗?"
  │
  ├─ planner: 识别为婚姻财产纠纷 → 制定 CRAG 三步计划
  │
  ├─ executor #1: 调用 retrieve_legal_knowledge → Pinecone 混合检索 → 返回 5 条判例
  ├─ executor #2: 调用 evaluate_case_relevance → 2条 correct, 1条 ambiguous → 判定不足
  │
  ├─ replan_check: 质量不充分 → 触发重规划
  ├─ replanner: 补充联网搜索 + 重新分析
  │
  ├─ executor #3: 调用 get_google_search → 获取最新司法解释
  ├─ executor #4: 调用 analyze_legal_issue → Saul Goodman 风格法律分析
  │
  ├─ replan_check: 已有 final_answer → 通过
  └─ finalize: 返回完整答案 + 援引案例 + 法条依据
```

---

## 人机协同 HITL

7 类中断-恢复交互，全部基于 `interrupt()` / PostgreSQL checkpoint 实现，服务端暂停等待、律师决策后无损续流：

| 中断类型 | 触发节点 | 场景 | 恢复选项 |
|----------|----------|------|----------|
| `ask_element` | ask_element | 案情要素缺失（如诉讼请求、子女情况），问诊补全 | 选项题 / 自由输入 |
| `mid_clarify` | mid_clarify | 执行中发现信息缺口，向律师中途澄清 | 选项题 / 自由输入 |
| `risk_confirm` | risk_gate | 高风险咨询识别（如涉嫌犯罪、人身安全） | 继续 / 终止 |
| `degrade_confirm` | hitl_degrade | 多轮澄清仍不足，确认降级执行 | 继续 / 终止 |
| `budget_confirm` | hitl_budget | 规划步数超预算 | 放行 / 精简 |
| `pdf_confirm` | executor | PDF 报告生成前确认 | 确认 / 跳过 |
| `docx_confirm` | executor | Word 文书生成前确认，附 74 字段预览卡（已填/待补充两态，关键字段缺失 amber 警示） | 确认 / 跳过 |

恢复答复经 `normalize_resume` 归一化（短词直接命中 / 语义判断兜底），由 `/ask/resume` 或 `/ask/resume/stream` 续跑图。前端 `InterruptPanel` 渲染所有类型卡片，服务端中断后 reload 会话可从 checkpoint 恢复现场。

---

## 检索系统

### 混合检索管线

```text
用户 Query
  ├─→ BGE-large-zh-v1.5 (密集向量, 1024 维)
  ├─→ BM25Encoder (稀疏向量, 预计算参数)
  └─→ convex_scale(alpha=0.7) 融合
       ↓
  Pinecone 混合查询 (dotproduct)
       ↓
  BGE-reranker-large CrossEncoder 重排序
       ↓
  返回 Top-N 结果
```

### 关键参数

| 组件 | 选型 | 说明 |
|------|------|------|
| 向量数据库 | Pinecone Serverless (us-east-1) | 密集 + 稀疏双向量存储 |
| 嵌入模型 | BAAI/bge-large-zh-v1.5 | 1024 维，中文语义优化，归一化输出 |
| 重排序 | BAAI/bge-reranker-large | CrossEncoder 全注意力架构 |
| 稀疏编码 | pinecone-text BM25Encoder | 预计算参数 ~387KB，启动即用 |
| 文本分割 | MarkdownHeaderTextSplitter + RecursiveCharTextSplitter | chunk_size=512, overlap=50 |

### 数据规模

- **11 个** 年度法院案例文件（2014-2024，婚姻家庭与继承纠纷）
- **约 5,030 条** 向量记录
- **7 部** 相关法律文本（民法典、反家暴法、妇女权益保障法等）

---

## 律师助理文书生成

律师助理模式下，`generate_docx` 工具以 **docxtpl + YAML 通用模板机制** 从案情描述生成标准 Word 法律文书（现为离婚民事起诉状，答辩状模板接口已预留）：

```text
案情描述 (case_details ≥20 字)
  │
  ▼ flash + 动态 Pydantic Schema (74 字段) 一次性抽取, 失败自动重试一次
  │
  ▼ docx_confirm interrupt → 律师预览 74 字段卡 (已填/待补充/关键缺失警示)
  │
  ▼ 确认 → docxtpl 渲染 (autoescape 防注入防崩溃, 缺失字段填"待补充", 勾选缺省全 ☐)
  │
  ▼ SSE docx_done 帧 → 前端进度弹窗收起 + 完成通知 + 下载入口
  │
  ▼ GET /sessions/{sid}/docx/latest (路径白名单防穿越)
```

- **模板资产**：`data/doc_templates/complaint/`（source.docx 原始模板 + fields.yaml 字段定义 + template.docx 注入产物），`scripts/build_docx_template.py` 可重建，anchor 未命中构建期报错
- **字段定义**：74 字段（54 文本 / 17 勾选 / 3 日期），YAML 声明 anchor 正则与替换规则，`doc_templates.py` 提供 YAML↔docx 标签双向对账
- **降级**：模板目录缺失/校验失败时整体降级 warning，planner 末步随之消失，回退纯咨询，不阻断流程

---

## 工具链

系统提供 **9 个本地工具**（另有 MCP server 挂载的 law-search 工具，掉线自动降级），通过 `langchain_core.tools` 注册，执行器按需调用：

| 工具 | 分类 | 功能 |
|------|------|------|
| `retrieve_legal_knowledge` | CRAG | Pinecone 混合检索法律案例，支持 top_k / alpha / namespace 参数调优 |
| `evaluate_case_relevance` | CRAG | 三档质量评估（correct ≥0.7 / ambiguous 0.3~0.7 / incorrect <0.3） |
| `analyze_legal_issue` | CRAG | 角色化法律分析生成，整合案例、法条与网络资料 |
| `get_google_search` | 外部搜索 | SerpAPI 联网搜索，最多 8 条结构化结果 |
| `search_memory` | 长期记忆 | PostgreSQL + pgvector 语义记忆搜索 (cosine 相似度) |
| `save_to_memory` | 长期记忆 | 保存用户事实/偏好，支持 memory_type 分类 |
| `fetch_laws` | 法条检索 | pgvector 法条语义检索 |
| `markdown_to_pdf` | 输出 | Markdown → HTML → PDF (A4, 中文字体)，前置 pdf_confirm HITL 确认 |
| `generate_docx` | 输出 | docxtpl 模板渲染 Word 法律文书（前置 docx_confirm HITL 确认），详见[律师助理文书生成](#律师助理文书生成) |

### 长期记忆

- **存储引擎**：PostgreSQL 15 + pgvector 扩展
- **嵌入维度**：1024（与 Pinecone 共用 BGE 模型）
- **表结构**：`agent_memory(id, thread_id, memory_type, content, embedding, metadata, created_at)`
- **搜索**：`cosine_similarity = 1 - (embedding <=> query_vec)`
- **设计要点**：原文存 metadata，摘要/截断文本用于向量嵌入；建表幂等；懒加载单例

---

## API 接口

| 端点 | 方法 | 功能 |
|------|------|------|
| `/attorney/ask` | POST | 代理律师模式同步问答（query 1-5000 字 + session_id） |
| `/attorney/ask/stream` | GET | 代理律师模式 SSE 流式问答（推理 token / CoT 思考流 / 计划 / 工具调用 / interrupt / docx_done） |
| `/assistant/ask` | POST | 律师助理模式同步问答（case_details ≥20 字，走文书生成链路） |
| `/assistant/ask/stream` | GET | 律师助理模式 SSE 流式问答 |
| `/assistant/ask/stream` | POST | 律师助理模式 SSE 流式问答（POST 变体，长案情走请求体） |
| `/sessions` | GET | 最近 50 条会话列表（session_id / meta / last_active_at） |
| `/sessions/{session_id}` | GET | 会话详情（图状态快照 + 当前 interrupt） |
| `/sessions/{session_id}/dialogue` | GET | 对话日志聚合：按轮次时间线还原问答/澄清/确认决策/终答/文书 |
| `/sessions/{session_id}/docx/latest` | GET | 下载本轮最新生成的 Word 文书（路径白名单，404 三态） |
| `/disclaimer` | GET | 免责声明文本 |
| `/ask/resume` | POST | HITL 恢复：interrupt 回复经 `normalize_resume` 归一后续跑图 |
| `/ask/resume/stream` | POST | HITL 恢复（SSE 流式版），interrupt 回复后续流推送 |
| `/feedback` | POST | 用户反馈提交 |
| `/ask` | POST | 同步问答（**deprecated** — 请改用 `/attorney/ask`，二期移除） |
| `/ask/stream` | GET | SSE 流式问答（**deprecated** — 请改用 `/attorney/ask/stream`，二期移除） |
| `/ask/pdf` | POST | 生成 PDF 法律报告并返回文件下载 |
| `/tools` | GET | 列出所有可用工具及参数描述 |
| `/home` | GET | 健康检查 + 服务信息 |

### 请求 / 响应

```python
# 请求
QueryRequest(
    query: str,           # 1-5000 字符
    session_id: str | None # 可选，支持多轮对话
)

# 响应
QueryResponse(
    query: str,
    session_id: str,
    final_answer: str,
    sources: list[str],
    tool_calls: list[ToolCallRecord],
    reasoning: str
)
```

---

## 数据模型

三层 Pydantic v2 模型体系，覆盖全数据流：

```python
# A. 工具返回层
RetrievedDocument  # rank, rerank_score, hybrid_score, year, case_number, chunk_text
EvaluationResult   # correct/ambiguous/incorrect 计数与列表, quality_verdict
WebSearchResult    # title, link, snippet

# B. 计划执行层
PlanStep           # step_id, description, tool_name, status, retry_count
ToolCallRecord     # step_id, tool_name, tool_input, output, timestamp

# C. 顶层 AgentState
AgentState         # 会话标识 · 请求上下文 · 计划执行 · 输出 · 管线数据 · 记忆 · 流控
```

---

## 日志系统

五类结构化日志，通过 `contextvars` 实现 session_id 全链路透传：

| Logger | 输出 | 级别 | 用途 |
|--------|------|------|------|
| `agent_flow` | 文件 (10MB 轮转) + 控制台 | INFO+ | 每次请求流程摘要 |
| `agent_debug` | 控制台 | DEBUG | 节点级执行链路 |
| `tool` | 控制台 | DEBUG | 工具调用参数 / 返回值 |
| `rag` | 控制台 | DEBUG | RAG 检索各环节耗时 |
| `system` | 文件 (轮转) + 控制台 | INFO+ | 启动 / 关闭 / 异常 |

控制台格式（带 ANSI 颜色）：

```text
15:37:22 | INFO  | sess_1234 | agent_flow | 执行完毕 | 调用了3个工具 | 成功
```

---

## 对话日志与追踪

### 对话日志（`dialogue_log.py` + `session_dialogue_events` 表）

- **事件流落库**：每轮问诊问题、用户答复、interrupt 决策、终答、docx 生成均以事件行落库，`seq` 原子生成保证顺序；写入为同步短连接、失败降级 warning 不阻断图流程
- **幂等守卫**：`round_question` 去重防 resume 重跑重复落库
- **聚合 API**：`GET /sessions/{sid}/dialogue` 按轮次时间线还原全部交互（问题/选项/确认决策/文书产物），前端会话抽屉渲染对话日志节，支持服务端中断后恢复现场

### LLM 追踪（`tracing.py` + trace 表）

- `traced` 装饰器包裹全部节点、工具与 LLM 调用，trace run/span 落 PostgreSQL（幂等建表）
- 前缀树聚合指标（节点耗时 / token / 错误率），兼容 LangSmith 遥测
- PG 掉线时 tracing 自动降级为空实现，零侵入业务

---

## 快速开始

### 环境要求

- Python 3.11+
- PostgreSQL 15（需 pgvector 扩展）
- wkhtmltopdf（PDF 生成依赖）

### 安装

```bash
# 1. 克隆仓库
git clone <repo-url> && cd LangChain_LawAgent-main

# 2. 安装依赖（在仓库根目录执行，requirements.txt 位于根目录）
pip install -r requirements.txt

# 3. 配置环境变量
cp lawApp_LangGraph/.env.example lawApp_LangGraph/.env
# 编辑 .env 填入 API Key 与数据库信息
```

### 环境变量

```ini
DEEPSEEK_API_KEY=sk-xxx
DEEPSEEK_BASE_URL=https://api.deepseek.com
PINECONE_API_KEY=xxx
PINECONE_INDEX_NAME=pinecone-law-agent   # 示例值，默认见 config.py (pinecone-test-lawapp)
SERPAPI_API_KEY=xxx
DB_NAME=Law_app
DB_USER=postgres
DB_PASSWORD=xxx
DB_HOST=localhost
DB_PORT=5433
```

其余可配置项（RECURSION_LIMIT / MAX_ROUNDS / CRAG 三档阈值 / 日志级别等）见 `docs/PROJECT_OVERVIEW.md` §15 配置一览表，字段与同名大写环境变量一一对应。

### 启动

在**仓库根目录**执行（`lawApp_LangGraph` 是包名，须从根目录导入）：

```bash
uvicorn lawApp_LangGraph.FastAPI.api:app --host 0.0.0.0 --port 8000 --reload
```

服务启动后访问 `http://localhost:8000/home` 验证健康状态。

> **Windows + PostgreSQL 注意**：psycopg_async 需要 Selector 事件循环，而 uvicorn 0.46 在
> win32 默认 Proactor 循环。使用 PG（checkpointer / 审计 / 检索）时须加 `--loop` 指定循环工厂
> （详见 `lawApp_LangGraph/FastAPI/loop.py`）：
>
> ```bash
> uvicorn lawApp_LangGraph.FastAPI.api:app --host 127.0.0.1 --port 8000 \
>   --loop lawApp_LangGraph.FastAPI.loop:selector_loop_factory
> ```

### 前端 (frontend/)

Vue 3 + Vite + Tailwind v4 单界面应用（双模式：代理律师咨询 / 律师助理文书起草）：

```bash
cd frontend
npm install
npm run dev      # http://localhost:5173（/api 前缀由 vite proxy 转发到 127.0.0.1:8000）
npm run build    # 产物 dist/，可由任意静态服务托管（/api 需网关承担同样转发）
```

---

## 项目结构

```text
LangChain_LawAgent-main/
├── README.md
├── requirements.txt             # Python 依赖（直接安装）
├── requirements.lock            # 锁定版本，复现环境用
├── environment.lock             # conda 环境导出
├── langgraph.json               # LangGraph CLI 配置
│
├── lawApp_LangGraph/            # 主体代码包（从仓库根目录导入）
│   ├── LangGraph_lawApp.py      # 主入口：StateGraph 定义 (14 节点, 含 HITL 中断)
│   ├── state.py                 # Pydantic 数据模型 (三层体系)
│   ├── config.py                # 运行时配置中心 (pydantic-settings, env 同名覆盖)
│   ├── prompts.py               # 提示词集中定义 (双模式 planner 后缀)
│   ├── runtime.py               # 运行时装配（工具 / 图 / 客户端）
│   ├── db.py                    # PostgreSQL + pgvector 连接层
│   ├── dialogue_log.py          # 对话日志事件流 (session_dialogue_events 落库 + 聚合)
│   ├── doc_templates.py         # docx 模板配置层 (YAML 加载 / 路径 / 双向对账 / 可用性)
│   ├── tracing.py               # LLM 追踪 (traced 装饰器, run/span 落库)
│   ├── mcp/                     # MCP 子包（law-search server + 客户端挂载）
│   │   ├── mcp_client.py        # MCP 客户端挂载，连接失败时优雅降级
│   │   └── mcp_server.py        # MCP server（http / stdio 两种传输）
│   ├── .env.example             # 环境变量模板
│   │
│   ├── FastAPI/                 # Web 服务层
│   │   ├── api.py               # FastAPI 应用 (v3.0.0)，18 端点 + CORS + 生命周期
│   │   ├── model.py             # 请求 / 响应模型
│   │   ├── utils.py             # 会话管理、interrupt 归一化、响应构建、SSE 事件队列
│   │   ├── loop.py              # Windows 事件循环工厂 (Selector)
│   │   └── logging.py           # 5类日志 + contextvars 会话透传
│   │
│   ├── tools/                   # Agent 工具集 (9 本地 + MCP)
│   │   ├── __init__.py          # 工具注册表 (LOCAL_TOOLS + MCP_TOOLS → ALL_TOOLS)
│   │   ├── tools.py             # SerpAPI 搜索 + PDF 生成 + Word 文书生成 (docxtpl)
│   │   ├── rag_tools.py         # CRAG 管线 (检索 / 评估 / 分析 + 角色提示词)
│   │   └── db_tools.py          # 长期记忆 + 法条检索 (pgvector)
│   │
│   └── RAG_service/             # RAG 检索服务
│       ├── RAG_program.py       # RAG_service 类：混合检索 + BM25 + CrossEncoder 重排序
│       ├── base.py              # 检索器抽象基类
│       ├── embedder.py          # BGE 嵌入 / 重排序模型懒加载单例
│       ├── pinecone_retriever.py # Pinecone 混合检索封装
│       └── pgvector_retriever.py # pgvector 法条检索封装
│
├── data/                        # 数据文件
│   ├── Documents/
│   │   ├── LawDocument/         # 7 部法律 TXT
│   │   └── MarkDownFiles/       # 11 个案例 MD (2014-2024)
│   ├── doc_templates/
│   │   └── complaint/           # Word 文书模板 (source.docx + fields.yaml + template.docx)
│   ├── bm25_law_params.json     # 预计算 BM25 参数（随仓库分发）
│   └── sample_law.txt
│
├── notebooks/                   # 实验与调试笔记本
│   ├── AgentTest.ipynb          # 主图端到端演练
│   ├── RAG_Service_Test.ipynb   # 混合检索验证
│   ├── clarify_test.ipynb       # 澄清节点
│   ├── hitl_test.ipynb          # Human-in-the-loop
│   ├── prompts_test.ipynb       # 提示词
│   └── tools_test.ipynb         # 工具集
│
├── tests/                       # pytest（157 用例，根目录执行 python -m pytest tests/ -q）
│   ├── test_engineering.py      # 配置 / 导入 / 工程约束
│   ├── test_smoke.py            # 冒烟（含图拓扑 / 工具注册表断言）
│   ├── test_boundary_*.py       # 边界用例（高/中/低后端）
│   ├── test_hitl_single_question.py # HITL 问诊 payload / resume 归一化
│   ├── test_dialogue_events.py  # 对话日志事件流 + 聚合（真实 PG）
│   ├── test_docx_generation.py  # Word 文书全链（模板/工具/图内/API, 28 用例）
│   ├── test_tracing.py / test_trace_db.py / test_trace_e2e.py # LLM 追踪
│   ├── test_mcp.py              # MCP 挂载与 stdio 端到端
│   └── ...
│
├── scripts/
│   ├── run_nb.py                # 批量执行 notebook
│   ├── ingest_cases_pgvector.py # 案例语料切块 + BGE 嵌入 + pgvector 入库
│   ├── build_docx_template.py  # Word 模板重建脚本 (source.docx → template.docx)
│   └── run_pytest_timeout_skip.py # PG 停机批次超时跳过执行器
│
├── frontend/                    # Vue 3 + Vite + Tailwind v4 单界面（双模式聊天）
│   ├── src/
│   │   ├── App.vue              # 单界面组装（模式切换 / SSE 分发 / HITL / docx 弹窗接线）
│   │   ├── api.js               # axios REST 封装
│   │   ├── sse.js               # fetch 流式 SSE 帧解析
│   │   ├── store.js             # reactive 全局状态 + 打字机 composable
│   │   └── components/          # 18 业务组件（ChatView / InterruptPanel / SessionDrawer
│   │                            #   / MarkdownView / DocxGenModal / DocxDoneToast 等）
│   └── vite.config.js           # /api proxy → 127.0.0.1:8000
│
└── docs/
    ├── PROJECT_OVERVIEW.md      # 设计文档 (§1-§15)
    └── superpowers/             # 历史设计与计划归档
```

运行时产物（`logs/`、`__pycache__/`、`.pytest_cache/`）不入库。

---

## 技术栈

| 层级 | 技术 | 说明 |
|------|------|------|
| Agent 框架 | LangGraph | StateGraph 编排 + MemorySaver 检查点 |
| LLM | DeepSeek (langchain-openai) | Pro 规划 / Flash 执行，双模型架构 |
| Web 服务 | FastAPI + Uvicorn | REST API + SSE 流式 |
| 数据模型 | Pydantic v2 | 全链路类型约束 |
| 向量检索 | Pinecone Serverless | 密集 + 稀疏混合查询 |
| 嵌入 & 重排 | BGE-large-zh-v1.5 / BGE-reranker-large | HuggingFace + sentence-transformers |
| 稀疏编码 | pinecone-text BM25Encoder | 关键词匹配 |
| 前端 | Vue 3 + Vite + Tailwind v4 | 单界面双模式（代理律师 / 律师助理），axios + fetch SSE |
| 长期记忆 | PostgreSQL 15 + pgvector | 语义记忆存取 |
| 联网搜索 | SerpAPI | Google 搜索结果结构化 |
| PDF 生成 | markdown + pdfkit (wkhtmltopdf) | Markdown → HTML → PDF |
| Word 文书 | docxtpl + YAML 模板 | Jinja2 标签注入 docx，动态 Pydantic Schema 字段抽取 |
| 工具扩展 | MCP (stdio/http) | law-search server 双向接入，掉线优雅降级 |
| 文本分割 | langchain-text-splitters | MarkdownHeader + RecursiveChar |
| 日志 | Python logging + contextvars | 5 类结构化日志 + 会话透传 |
| 追踪 | traced 装饰器 + PostgreSQL trace 表 + LangSmith | 节点/工具/LLM run-span 全链路观测 |

---

<div align="center">
  <sub>Built with LangGraph · DeepSeek · Pinecone · FastAPI · Vue3</sub>
</div>
