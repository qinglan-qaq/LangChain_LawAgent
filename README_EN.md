# lawApp_LangGraph — Legal AI Consultation Agent

**[English](README_EN.md)** | **[中文](README.md)**

<div align="center">

A marriage & family law AI consultation system built on the **LangGraph Plan & Execute** architecture, integrating hybrid retrieval-augmented generation with CRAG (Corrective RAG), human-in-the-loop (HITL) approval, long-term memory, Word legal document generation, dialogue-log auditing, and a Vue3 streaming frontend.

[![Python](https://img.shields.io/badge/Python-3.11-3776AB?style=flat&logo=python)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.136-009688?style=flat&logo=fastapi)](https://fastapi.tiangolo.com/)
[![LangGraph](https://img.shields.io/badge/LangGraph-1.0-1C3C3C?style=flat&logo=langchain)](https://langchain-ai.github.io/langgraph/)
[![Pinecone](https://img.shields.io/badge/Pinecone-Serverless-1C17FF?style=flat)](https://www.pinecone.io/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-15-4169E1?style=flat&logo=postgresql)](https://www.postgresql.org/)
[![License](https://img.shields.io/badge/License-MIT-yellow?style=flat)](LICENSE)

</div>

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Highlights](#highlights)
- [Agent Workflow](#agent-workflow)
- [Human-in-the-Loop (HITL)](#human-in-the-loop-hitl)
- [Retrieval System](#retrieval-system)
- [Paralegal Document Generation](#paralegal-document-generation)
- [Chat Experience](#chat-experience)
- [Tool Chain](#tool-chain)
- [API Endpoints](#api-endpoints)
- [Data Models](#data-models)
- [Logging System](#logging-system)
- [Dialogue Logs & Tracing](#dialogue-logs--tracing)
- [Quick Start](#quick-start)
- [Project Structure](#project-structure)
- [Tech Stack](#tech-stack)

---

## Architecture Overview

The system follows the **Plan & Execute** paradigm: a strong model drafts the execution plan, lightweight models execute it step by step — maintaining answer quality while dramatically cutting inference cost.

```text
User request → FastAPI → LangGraph StateGraph
                        │
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼
    planner         executor        replan_check
  (DeepSeek Pro)  (DeepSeek Flash)  (DeepSeek Flash)
        │               │               │
        │   ┌───────────┘         ┌─────┴─────┐
        │   ▼                     ▼           ▼
        │  tool calls          finalize    replanner
        │  ├─ Pinecone hybrid retrieval    │
        │  ├─ pgvector statute search      (DeepSeek Pro)
        │  ├─ SerpAPI web search            │
        │  ├─ PostgreSQL memory             ▼
        │  ├─ PDF report               executor
        │  └─ Word document
        │   │
        │   ▼ interrupt() ──→ frontend HITL card ──→ /ask/resume continues the stream
        └───────────────────────────┘
```

**Dual-LLM cost optimization**: planning/replanning use DeepSeek Pro (strong reasoning), execution/quality gating use DeepSeek Flash (low cost, low latency) — saving **40–60% token consumption** per query.

---

## Highlights

### Architecture

| Highlight | Description |
|------|------|
| **Flexible Plan & Execute pipeline** | The agent decides autonomously by question complexity — chitchat skips execution and answers directly; legal questions run the full CRAG pipeline |
| **HITL approval** | 7 interrupt-resume interactions (intake completion / mid-run clarification / high-risk / degradation / budget overrun / PDF report / Word document confirmation); after the lawyer's decision the stream resumes losslessly from PostgreSQL checkpoints |
| **Three-level graceful degradation** | JSON parse failure → default plan fallback; LLM call failure → parameter-mapping fallback; semantic judgment failure → rule fallback; plus MCP-offline degradation and whole-chain document-template fallback to pure consultation |
| **Quality loop** | Retrieve → evaluate (three tiers) → supplement with web search if insufficient → replan → re-execute: a self-correcting feedback loop |
| **Dual mode + paralegal** | Attorney-consultation mode (case + statute dual retrieval) and paralegal mode (statutes only, no similar-case lookup, final step generates a document) share one graph |
| **End-to-end type safety** | Pydantic v2 covers the full data flow: tool returns → graph state → API responses |

### Retrieval-Augmented Generation

| Highlight | Description |
|------|------|
| **RRF dual-route fusion** | Dense semantic recall (BGE) and sparse keyword recall (BM25) query independently, then fuse client-side via Reciprocal Rank Fusion (k=60, tunable weights) — rank-level fusion is naturally immune to score-scale differences, no α weight tuning needed |
| **CrossEncoder rerank** | BGE-reranker-large full-attention interaction scoring; logits pass through sigmoid into relevance_score (0–1), the sole basis for CRAG tiering |
| **CRAG three-tier evaluation** | Cut points empirically calibrated on a golden set (correct ≥0.731 / 0.5005 ≤ ambiguous < 0.731 / incorrect < 0.5005); the agent autonomously decides whether to supplement with web search |
| **Honest degradation** | Single-route recall failure degrades to the surviving route; when rerank is unavailable docs lack the relevance_score key → evaluation puts all in ambiguous + forcibly reports "insufficient", never faking adequacy with rank scores |

### Engineering

| Highlight | Description |
|------|------|
| **Config center** | pydantic-settings single entry (`config.py`); process env > `.env` > defaults, one-to-one with same-named uppercase variables |
| **Lazy-loaded singletons** | RAG service, embedding models, LLMs, DB pool all lazy-init with `threading.Lock` double-checked guarding cold-start races |
| **5 structured loggers** | session_id propagated through the whole chain via contextvars; file logs are JSON lines with structured fields, ready for log collectors |
| **SSE streaming** | Real-time push of reasoning tokens, plan progress, tool calls, interrupts, document events — zero-refresh full-process visibility in the frontend |
| **Dialogue-log audit** | Full event stream persisted (Q&A / clarification / confirmation decisions / final answer / documents), aggregate API restores per-round timelines; frontend recovers losslessly after server interruption |
| **LLM tracing** | `traced` decorator wraps all nodes/tools/LLM calls; trace runs/spans stored in PostgreSQL, LangSmith-compatible |
| **Test-backed** | 34 test files, 311 cases (real PostgreSQL end-to-end + in-graph flows + security boundaries + RRF fusion/retrieval/calibration), all green |
| **Concurrency safety** | Session-level isolation and interrupt recovery built on LangGraph PostgreSQL checkpoints |

---

## Agent Workflow

`LangGraph StateGraph` contains **16 nodes** covering intake, risk gating, the CRAG execution loop, execution-time element completion, and HITL interrupts:

### Nodes

| Node | Model | Responsibility |
|------|------|------|
| `ingest` | — | Session init, per-round state reset, history memory loading |
| `risk_gate` | Flash | High-risk consultation detection, triggers interrupt for lawyer confirmation |
| `chitchat` | Flash | Chitchat short-circuit — answers directly, skips the pipeline |
| `ask_element` | Flash | Case-element intake completion (can raise interrupt option cards) |
| `element_fill` | — | Execution-time element completion: planner emits pseudo-tool `request_element_fill`; the node interrupts, writes the answer to `user_supplements` / `[补充]` message and advances the step |
| `planner` | DeepSeek Pro (t=0.4, 4096 tokens) | Analyzes the question, outputs JSON reasoning chain + step plan |
| `executor` | DeepSeek Flash (t=0.25, 2048 tokens) | Calls tools per step; supports LLM function calling with parameter-mapping fallback |
| `tools` | — | ToolNode executes tool calls uniformly and writes back state |
| `merge` | — | Parses tool outputs, merges keys declared in _STATE_KEYS back into graph state |
| `replan_check` | DeepSeek Flash | Evaluates execution quality (case count, evaluation result, errors), decides whether to replan |
| `mid_clarify` | Flash | Mid-execution information gap → asks the lawyer for clarification (interrupt) |
| `hitl_degrade` | Flash | Multiple clarification rounds still insufficient → asks the lawyer to confirm degraded execution (interrupt) |
| `hitl_budget` | Flash | Planned steps exceed budget → asks the lawyer to release (interrupt) |
| `replanner` | DeepSeek Pro | Generates supplementary plans from executed steps (no repeats, max 10 rounds) |
| `finalize` | — | Assembles the final answer (with citations); uses existing answer if present, otherwise LLM fallback |

### Routing

```text
START → ingest → risk_gate ──[risk confirmed]──→ interrupt → resume
                      │
                      └─→ chitchat ──[chitchat]──→ END
                          │
                          └─→ ask_element ─(interrupt)─→ planner ──[plan empty]──→ finalize → END
                                          │
                          └──[has steps]──→ executor ─[tool_calls]→ tools → merge ─┐
                                                          │  └─[interrupt: pdf/docx/degrade/budget]─┤
                                                          ▼                                    │
                                                  [steps remaining]→ executor (loop)              │
                                                          │                                    │
                                                          ▼                                    │
                                                      replan_check ─[pass]→ mid_clarify → finalize → END
                                                          │
                                                          └─[fail]→ replanner → executor
```

### Multi-turn Example

```text
User: "Can I get my betrothal gifts back after divorce?"
  │
  ├─ planner: marital property dispute → 3-step CRAG plan
  │
  ├─ executor #1: retrieve_legal_knowledge → RRF hybrid search → 5 precedents
  ├─ executor #2: evaluate_case_relevance → 2 correct, 1 ambiguous → insufficient
  │
  ├─ replan_check: quality insufficient → trigger replan
  ├─ replanner: supplement web search + re-analyze
  │
  ├─ executor #3: get_google_search → latest judicial interpretations
  ├─ executor #4: analyze_legal_issue → Saul Goodman style legal analysis
  │
  ├─ replan_check: final_answer present → pass
  └─ finalize: full answer + cited cases + statutory basis
```

---

## Human-in-the-Loop (HITL)

8 interrupt-resume interactions, all built on `interrupt()` / PostgreSQL checkpoints — the server pauses and waits, the stream resumes losslessly after the lawyer's decision:

| Interrupt | Node | Scenario | Resume options |
|----------|------|----------|----------|
| `ask_element` | ask_element | Missing case elements (claims, children, etc.), intake completion | Options / free text |
| `element_fill` | element_fill | Key element gap found during execution (planner pseudo-tool step) | Free text / skip |
| `mid_clarify` | mid_clarify | Information gap found mid-execution | Options / free text |
| `risk_confirm` | risk_gate | High-risk consultation (suspected crime, personal safety) | Continue / abort |
| `degrade_confirm` | hitl_degrade | Clarification still insufficient after rounds, confirm degraded execution | Continue / abort |
| `budget_confirm` | hitl_budget | Planned steps exceed budget | Release / trim |
| `pdf_confirm` | executor | Confirm before PDF report generation | Confirm / skip |
| `docx_confirm` | executor | Confirm before Word document generation, with a 74-field preview card (filled/to-fill states, amber warning for missing key fields) | Confirm / skip |

Resume answers are normalized by `normalize_resume` (short-word exact match / semantic-judgment fallback), then the graph continues via `/ask/resume` or `/ask/resume/stream`. The frontend `InterruptPanel` renders all card types; after a server-side interruption, reloading the session restores the scene from checkpoint.

---

## Retrieval System

### RRF Hybrid Retrieval Pipeline

```text
User query
  ├─→ dense_recall:  BGE-large-zh-v1.5 (dense vector, 1024-dim) → Pinecone semantic query
  ├─→ sparse_recall: BM25Encoder (sparse vector, precomputed params) → Pinecone keyword query
  │        (both routes run concurrently via asyncio.gather)
  ▼
rrf_fuse Reciprocal Rank Fusion: rrf(d) = Σ w/(k + rank_route),  k=60, tunable weights
  │        (keeps the fused top max(2×rerank_top_n, 20) candidates)
  ▼
BGE-reranker-large CrossEncoder rerank → sigmoid(logits) → relevance_score
  ▼
Top-N returned (default 5): relevance_score = decision basis, rrf_score = provenance
```

### Score Semantics

| Field | Source | Purpose |
|------|------|------|
| `relevance_score` | sigmoid(CrossEncoder logits) | **Sole basis for CRAG tiering** (absolute score — can detect "all garbage") |
| `hybrid_score` | alias of relevance_score | Legacy monitoring/scoring code (`tracing.py` / `score.py`) keeps working unchanged |
| `rrf_score` | Σ w/(k+rank) | Provenance and monitoring display only, never used in decisions — rank-relative, junk recalls still score near-max |

RRF fusion only decides *which candidates enter rerank*; final ordering and quality gating are entirely up to the CrossEncoder absolute score.

### Degradation Semantics

| Failure | Behavior |
|------|------|
| One recall route fails | Degrade to the surviving route (warning log); both routes fail → raise → tool layer returns the fixed Chinese message |
| Rerank unavailable | Docs lack the relevance_score/hybrid_score keys (missing key = marker) → evaluation puts all in ambiguous + quality_verdict forcibly "insufficient, web search supplement recommended" |

The three-tier thresholds are empirically calibrated on a golden set (`scripts/run_eval.py --suite calibration`, label `rrf-cal-1`): correct ≥0.731 (highest cut with precision ≥0.7), incorrect <0.5005 (cut where below-relevant share <5%); overridable via `CORRECT_THRESHOLD` / `INCORRECT_THRESHOLD`.

### Key Components

| Component | Choice | Notes |
|------|------|------|
| Vector DB | Pinecone Serverless (us-east-1) | Dense + sparse dual-vector storage (do NOT pass vector_type when creating indexes — a pure-dense index rejects sparse queries) |
| Embedding | BAAI/bge-large-zh-v1.5 | 1024-dim, Chinese-optimized, normalized output |
| Reranker | BAAI/bge-reranker-large | CrossEncoder full-attention, client-side lazy singleton |
| Sparse encoder | pinecone-text BM25Encoder | Precomputed ~387KB params shipped with the repo, ready at startup |
| RRF fusion | pure functions in `fusion.py` | k=60 (`RRF_K`), dense/sparse weights 1.0 (`RRF_W_DENSE`/`RRF_W_SPARSE`) |
| Text splitting | MarkdownHeaderTextSplitter + RecursiveCharTextSplitter | chunk_size=512, overlap=50 |

### Data Scale

- **11** annual court case files (2014–2024, marriage/family and inheritance disputes)
- **~5,030** vector records
- **7** relevant legal texts (Civil Code, Anti-Domestic-Violence Law, Women's Rights Law, etc.)

### Retrieval Evaluation

```bash
python scripts/gen_golden_set.py            # generate golden set (30 cases, stratified by cause of action)
python scripts/run_eval.py --suite retrieval --label <label> [--baseline <old-label>]   # metric regression gate
python scripts/run_eval.py --suite calibration --label <label>                          # tier-threshold calibration
```

`eval_metrics.py` provides hit_rate@k / MRR@k / P / R / F1 plus calibration helpers (split_scores / suggest_thresholds); eval results are persisted to the `eval_runs` table for the monitoring page.

---

## Paralegal Document Generation

In paralegal mode, the `generate_docx` tool uses a **docxtpl + YAML generic template mechanism** to produce a standard Word legal document (currently a divorce civil complaint; a defense-statement template interface is reserved) from case descriptions:

```text
Case description (case_details ≥20 chars)
  │
  ▼ flash + dynamic Pydantic schema (74 fields) one-shot extraction, auto retry once on failure
  │
  ▼ docx_confirm interrupt → lawyer previews the 74-field card (filled/to-fill/missing-key warnings)
  │
  ▼ Confirm → docxtpl render (autoescape injection-safe, missing fields → "to be filled", unchecked → ☐)
  │
  ▼ SSE docx_done frame → frontend progress modal closes + completion toast + download entry
  │
  ▼ GET /sessions/{sid}/docx/latest (path whitelist against traversal)
```

- **Template assets**: `data/doc_templates/complaint/` (source.docx original + fields.yaml definitions + template.docx injected product); rebuildable via `scripts/build_docx_template.py`, anchor misses fail at build time
- **Field definitions**: 74 fields (54 text / 17 checkbox / 3 date); YAML declares anchor regexes and replacement rules; `doc_templates.py` provides YAML↔docx tag reconciliation
- **Degradation**: template directory missing/validation failure → overall warning-level degradation, the planner's final step disappears, falls back to pure consultation, never blocks the flow

---

## Chat Experience

The chat page (Vue3 + Tailwind4) is built around seven capabilities of "visible process, controllable state":

```text
● ready → thinking → 🔧 calling {tool} → composing (backend status) → streaming → ● ready
```

- **Per-message thinking panel**: each assistant bubble has its own collapsible thinking section (status / progress / plan / tool calls / CoT / prompt snapshot), auto-expanded while streaming, collapsed on completion, re-openable anytime (`MessageThinking.vue`)
- **Explicit tool-call indicators**: `🔧 calling {tool}` spinner → `✓ {tool} · result summary`
- **Fixed frontend disclaimer**: prompt templates no longer ask the LLM to append it — a fixed gray footer line under every completed answer
- **Conditional case-element panel**: `elements` SSE frames carry `is_case_query` (derived from `question_category != "chitchat"`); hidden for chitchat rounds
- **Agent status bar**: persistent above the footer (`AgentStatusBar.vue`), pure frontend aggregation of existing SSE states
- **Card-style composer**: attorney input + send on one line, assistant document-mode radio pills + hint line, centered `max-w-3xl` conversation column
- **Short-term memory loop**: write side persists user messages each round + assistant messages on `finalize`/`chitchat` (idempotent); read side `_query_with_supplements` assembles the last 3 rounds (filtering empty placeholders, 300 chars/item / 1200 chars total cap) — planner / executor / replan_check / finalize / chitchat all recall context

---

## Tool Chain

The system provides **9 local tools** (plus a law-search tool mounted from an MCP server, gracefully degraded when offline), registered via `langchain_core.tools` and called by the executor on demand:

| Tool | Category | Function |
|------|------|------|
| `retrieve_legal_knowledge` | CRAG | RRF hybrid case retrieval (dense+BM25 → RRF fusion → CrossEncoder rerank); tunable top_k / rerank_top_n / namespace |
| `evaluate_case_relevance` | CRAG | Three-tier quality evaluation (basis: relevance_score — correct ≥0.731 / ambiguous ≥0.5005 / incorrect); rerank unavailable → all ambiguous + forced "insufficient" |
| `analyze_legal_issue` | CRAG | Role-styled legal analysis generation, integrating cases, statutes and web materials |
| `get_google_search` | External | SerpAPI web search, up to 8 structured results |
| `search_memory` | Long-term memory | PostgreSQL + pgvector semantic memory search (cosine similarity) |
| `save_to_memory` | Long-term memory | Save user facts/preferences, memory_type classification |
| `fetch_laws` | Statute retrieval | pgvector semantic statute search |
| `markdown_to_pdf` | Output | Markdown → HTML → PDF (A4, CJK fonts), preceded by pdf_confirm HITL |
| `generate_docx` | Output | docxtpl template-rendered Word legal document (preceded by docx_confirm HITL), see [Paralegal Document Generation](#paralegal-document-generation) |

### Long-term Memory

- **Engine**: PostgreSQL 15 + pgvector extension
- **Embedding dim**: 1024 (shares the BGE model with Pinecone)
- **Schema**: `agent_memory(id, thread_id, memory_type, content, embedding, metadata, created_at)`
- **Search**: `cosine_similarity = 1 - (embedding <=> query_vec)`
- **Design**: original text in metadata, summary/truncated text for embedding; idempotent DDL; lazy singleton

---

## API Endpoints

| Endpoint | Method | Function |
|------|------|------|
| `/attorney/ask` | POST | Attorney-mode sync Q&A (query 1–5000 chars + session_id) |
| `/attorney/ask/stream` | GET | Attorney-mode SSE streaming (reasoning tokens / CoT / plan / tool calls / interrupts / docx_done) |
| `/assistant/ask` | POST | Paralegal-mode sync Q&A (case_details ≥20 chars, document-generation chain) |
| `/assistant/ask/stream` | GET | Paralegal-mode SSE streaming |
| `/assistant/ask/stream` | POST | Paralegal-mode SSE streaming (POST variant for long case texts) |
| `/sessions` | GET | Last 50 sessions (session_id / meta / last_active_at) |
| `/sessions/{session_id}` | GET | Session detail (graph state snapshot + current interrupt) |
| `/sessions/{session_id}/dialogue` | GET | Dialogue-log aggregation: per-round timeline of Q&A / clarifications / decisions / answers / documents |
| `/sessions/{session_id}/docx/latest` | GET | Download the latest generated Word document (path whitelist, 404 states) |
| `/disclaimer` | GET | Disclaimer text |
| `/ask/resume` | POST | HITL resume: interrupt reply normalized by `normalize_resume`, graph continues |
| `/ask/resume/stream` | POST | HITL resume (SSE streaming variant) |
| `/feedback` | POST | Submit user feedback |
| `/ask` | POST | Sync Q&A (**deprecated** — use `/attorney/ask`, removal in phase 2) |
| `/ask/stream` | GET | SSE streaming Q&A (**deprecated** — use `/attorney/ask/stream`, removal in phase 2) |
| `/ask/pdf` | POST | Generate a PDF legal report and return the file download |
| `/tools` | GET | List all available tools with parameter descriptions |
| `/home` | GET | Health check + service info |

### Request / Response

```python
# Request
QueryRequest(
    query: str,            # 1-5000 chars
    session_id: str | None # optional, multi-turn support
)

# Response
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

## Data Models

A three-layer Pydantic v2 model system covering the whole data flow:

```python
# A. Tool return layer
RetrievedDocument  # rank, id, rrf_score (provenance), relevance_score (decision basis),
                   # hybrid_score (compat alias), year, case_number, case_cause, chunk_text
EvaluationResult   # correct/ambiguous/incorrect counts & lists, quality_verdict
WebSearchResult    # title, link, snippet

# B. Plan-execution layer
PlanStep           # step_id, description, tool_name, status, retry_count
ToolCallRecord     # step_id, tool_name, tool_input, output, timestamp

# C. Top-level AgentState
AgentState         # session ids · request context · plan execution · outputs · pipeline data · memory · flow control
```

---

## Logging System

Five structured loggers; session_id propagated chain-wide via `contextvars`:

| Logger | Output | Level | Purpose |
|--------|------|------|------|
| `agent_flow` | file (10MB rotating) + console | INFO+ | Per-request flow summary |
| `agent_debug` | console | DEBUG | Node-level execution chains |
| `tool` | console | DEBUG | Tool call params / returns |
| `rag` | console | DEBUG | RAG retrieval stage timings |
| `system` | file (rotating) + console | INFO+ | Startup / shutdown / exceptions |

Console format (ANSI colored):

```text
15:37:22 | INFO  | sess_1234 | agent_flow | done | 3 tools called | success
```

---

## Dialogue Logs & Tracing

### Dialogue logs (`dialogue_log.py` + `session_dialogue_events` table)

- **Event-stream persistence**: every round's questions, user answers, interrupt decisions, final answers, docx generations are persisted as event rows with atomically generated `seq` ordering; writes are short sync connections that degrade to warnings without blocking the graph
- **Idempotent guards**: `round_question` dedup prevents duplicate rows on resume re-runs
- **Aggregate API**: `GET /sessions/{sid}/dialogue` restores full interaction timelines per round; the frontend session drawer renders the dialogue-log section, recovering the scene after server interruptions

### LLM tracing (`tracing.py` + trace tables)

- `traced` decorator wraps all nodes, tools and LLM calls; trace runs/spans stored in PostgreSQL (idempotent DDL)
- Prefix-tree aggregated metrics (node latency / tokens / error rates), LangSmith-compatible
- When PG is offline, tracing degrades to a no-op implementation with zero business intrusion

### Node-level content monitoring (`minor_state.py` + `minor_states`)

- **1:1 content extension**: every `stage_chain` row (SCD2 lifecycle) has one `minor_states` row with input/output projections, `output_json`, context-envelope meta, route, error and refs; missing rows are synthesized as placeholders so the timeline never shows empty rows
- **Single-transaction writer**: `MonitorWriter` uses one queue / one consumer / one transaction per batch; a full queue drops observations and never blocks the main flow
- **Projection safety**: whitelist + blacklist sanitization, 2000-char text / 64KB JSON caps; raw detail is referenced back to `trace_spans`
- **Chained timeline**: `GET /monitor/runs/{run_id}/timeline?chain=1` follows `parent_run_id` up to 3 layers and inserts a "user answer" divider between HITL resumes; returns execution / wait-human / overhead composition
- **Incremental + detail**: `GET /monitor/runs/{run_id}/minor-states?after_stage_id=` for 2s frontend polling; `GET /monitor/stages/{stage_id}/detail` returns content + raw span + full final answer; `GET /monitor/config` ships calibrated thresholds and text limits
- **Frontend**: vertical timeline + duration composition bars + detail panel below the timeline (`MonitorTimeline` / `MinorStateCard` / `MinorStateDetail` / `ContextMetaPanel`), with `#run=<id>&stage=<id>` deep links

---

## Quick Start

### Requirements

| Dependency | Version / Notes | Used for |
|------|------|------|
| Python | 3.11+ | Backend runtime (pinned versions in `requirements.lock` / conda export `environment.lock`) |
| Node.js | 18+ | Frontend build (Vue 3 + Vite 7 + Tailwind v4) |
| PostgreSQL 15 + pgvector | docker recommended | Session checkpoints / dialogue logs / LLM tracing / long-term memory / statute search / eval records |
| Pinecone | Serverless account | Case hybrid retrieval (dense+sparse dual-vector index) |
| DeepSeek API key | — | LLM planning (Pro) and execution (Flash) |
| SerpAPI key | optional | Web-search supplement (without it, insufficient-retrieval answers just lack this source) |
| wkhtmltopdf | — | PDF report generation |
| HuggingFace models | auto-downloaded on first run | `BAAI/bge-large-zh-v1.5` (~1.3GB, embedding) + `BAAI/bge-reranker-large` (~2.2GB, rerank); cache via `HF_HOME`; `HF_ENDPOINT` mirror for mainland China |

### Installation

```bash
# 1. Clone
git clone <repo-url> && cd LangChain_LawAgent-main

# 2. Install Python dependencies (run from repo root; requirements.txt is at root)
pip install -r requirements.txt
# or reproduce pinned versions: pip install -r requirements.lock

# 3. Start PostgreSQL (pgvector)
docker run -d --name postgres-vector \
  -e POSTGRES_USER=my_pgsql -e POSTGRES_PASSWORD=123123 -e POSTGRES_DB=Law_app \
  -p 15432:5432 ankane/pgvector:latest

# 4. Configure environment
cp lawApp_LangGraph/.env.example lawApp_LangGraph/.env
# edit .env: fill in API keys and DB connection (see below)
```

### Environment Variables

`lawApp_LangGraph/.env` (read by pydantic-settings; priority: process env > .env > `config.py` defaults):

```ini
# —— Required ——
DEEPSEEK_API_KEY=sk-xxx
DEEPSEEK_BASE_URL=https://api.deepseek.com
PINECONE_API_KEY=xxx                      # case hybrid retrieval
DB_HOST=localhost
DB_PORT=15432
DB_NAME=Law_app
DB_USER=my_pgsql
DB_PASSWORD=xxx

# —— As needed ——
PINECONE_INDEX_NAME=pinecone-test-lawapp  # index name
PINECONE_NAMESPACE=Law_test_namespace      # namespace
SERPAPI_API_KEY=xxx                       # web search (optional)
RETRIEVER_BACKEND=pinecone                # pinecone (default) / pgvector
HF_HOME=E:/huggingface_cache               # model cache dir
MCP_SERVER_URL=http://127.0.0.1:9381/mcp  # MCP tool mount (auto-degrades offline)

# —— Tunables (all have defaults; same-named uppercase overrides config.py) ——
# RRF_K=60 / RRF_W_DENSE=1.0 / RRF_W_SPARSE=1.0       # RRF fusion
# CORRECT_THRESHOLD=0.731 / INCORRECT_THRESHOLD=0.5005  # CRAG tiers (golden-set calibrated)
# RECURSION_LIMIT=60 / MAX_ROUNDS=10 / MAX_CLARIFY_ROUNDS=5
```

Full config list (30+ items): `docs/PROJECT_OVERVIEW.md` §15.

### Data Ingestion (first deployment)

```bash
# A. Pinecone case library (hybrid retrieval): create hybrid index + full upload
#    create_index() now creates a hybrid-capable index (do NOT pass vector_type="dense")
#    Upload entry: add_document() in lawApp_LangGraph/RAG_service/RAG_program.py
#    (Markdown chunking → BGE dense + BM25 sparse vectors → batched upsert; see notebooks/RAG_Service_Test.ipynb)

# B. pgvector case library (statute search / fallback backend)
python scripts/ingest_cases_pgvector.py          # idempotent; reruns upsert by id

# C. Evaluation golden set (optional, 30 cases)
python scripts/gen_golden_set.py
```

### Start the Service

Run from the **repo root** (`lawApp_LangGraph` is the package name and must be imported from root):

```bash
uvicorn lawApp_LangGraph.FastAPI.api:app --host 0.0.0.0 --port 8000 --reload
```

Then visit `http://localhost:8000/home` to verify health.

> **Windows + PostgreSQL note**: psycopg_async requires a Selector event loop, but uvicorn 0.46 defaults to
> Proactor on win32. When using PG (checkpointer / audit / retrieval), pass the loop factory
> (see `lawApp_LangGraph/FastAPI/loop.py`):
>
> ```bash
> uvicorn lawApp_LangGraph.FastAPI.api:app --host 127.0.0.1 --port 8000 \
>   --loop lawApp_LangGraph.FastAPI.loop:selector_loop_factory
> ```

### Frontend (frontend/)

Vue 3 + Vite + Tailwind v4 single-page app (dual mode: attorney consultation / paralegal drafting):

```bash
cd frontend
npm install
npm run dev      # http://localhost:5173 (/api proxied by vite to 127.0.0.1:8000)
npm run build    # output dist/, hostable by any static server (/api needs the same forwarding at the gateway)
```

### MCP server (optional)

```bash
python -m lawApp_LangGraph.mcp.mcp_server          # default 127.0.0.1:9381
MCP_PORT=9382 python -m lawApp_LangGraph.mcp.mcp_server
```

The law-search tool is mounted via MCP (http / stdio transports); if the server goes offline the client degrades gracefully without blocking the main flow.

### Tests & Evaluation

```bash
python -m pytest tests/ -q                  # full regression (311 cases, requires PG online)
python scripts/run_eval.py --suite retrieval --label <label> [--baseline <old-label>]  # retrieval metrics
python scripts/run_eval.py --suite calibration --label <label>                          # tier-threshold calibration
```

---

## Project Structure

```text
LangChain_LawAgent-main/
├── README.md / README_EN.md     # Chinese / English documentation
├── requirements.txt             # Python dependencies (direct install)
├── requirements.lock            # pinned versions for reproducibility
├── environment.lock             # conda environment export
├── langgraph.json               # LangGraph CLI config
│
├── lawApp_LangGraph/            # main package (import from repo root)
│   ├── LangGraph_lawApp.py      # main entry: StateGraph definition (16 nodes, HITL interrupts)
│   ├── state.py                 # Pydantic data models (three-layer system)
│   ├── config.py                # runtime config center (pydantic-settings, env override)
│   ├── prompts.py               # centralized prompts (dual-mode planner suffixes)
│   ├── runtime.py               # runtime assembly (tools / graph / clients)
│   ├── db.py                    # PostgreSQL + pgvector connection layer
│   ├── dialogue_log.py          # dialogue-log event stream (session_dialogue_events + aggregation)
│   ├── doc_templates.py         # docx template config (YAML loading / paths / reconciliation)
│   ├── tracing.py               # LLM tracing (traced decorator, runs/spans persisted)
│   ├── eval_metrics.py          # retrieval metrics (hit_rate/MRR/P/R/F1 + threshold calibration helpers)
│   ├── mcp/                     # MCP subpackage (law-search server + client mount)
│   │   ├── mcp_client.py        # MCP client mount, graceful degradation on connect failure
│   │   └── mcp_server.py        # MCP server (http / stdio transports)
│   ├── .env / .env.example      # env files / template
│   │
│   ├── FastAPI/                 # web service layer
│   │   ├── api.py               # FastAPI app (v3.0.0), 18 endpoints + CORS + lifecycle
│   │   ├── model.py             # request / response models
│   │   ├── utils.py             # session mgmt, interrupt normalization, response building, SSE queues
│   │   ├── loop.py              # Windows event-loop factory (Selector)
│   │   └── logging.py           # 5 loggers + contextvars session propagation
│   │
│   ├── tools/                   # agent tools (9 local + MCP)
│   │   ├── __init__.py          # tool registry (LOCAL_TOOLS + MCP_TOOLS → ALL_TOOLS)
│   │   ├── tools.py             # SerpAPI search + PDF generation + Word documents (docxtpl)
│   │   ├── rag_tools.py         # CRAG pipeline (retrieve / evaluate / analyze + role prompts)
│   │   └── db_tools.py          # long-term memory + statute search (pgvector)
│   │
│   └── RAG_service/             # RAG retrieval service
│       ├── RAG_program.py       # RAG_service class: index/chunking/upload + dense_recall/sparse_recall
│       ├── base.py              # retriever abstract base (get_retriever dispatch by RETRIEVER_BACKEND)
│       ├── fusion.py            # RRF fusion pure functions (rrf_fuse / sigmoid / clamp_search_params)
│       ├── embedder.py          # BGE embedding / rerank model lazy singletons
│       ├── pinecone_retriever.py # Pinecone orchestration: concurrent dual-route recall → RRF → CrossEncoder scoring
│       └── pgvector_retriever.py # pgvector statute retriever (dense-only, outside RRF redesign)
│
├── data/                        # data files
│   ├── Documents/
│   │   ├── LawDocument/         # 7 law texts (TXT)
│   │   └── MarkDownFiles/       # 11 case files (MD, 2014-2024)
│   ├── doc_templates/
│   │   └── complaint/           # Word template (source.docx + fields.yaml + template.docx)
│   ├── eval/                    # golden set (retrieval.jsonl, regenerable)
│   ├── bm25_law_params.json     # precomputed BM25 params (shipped with repo)
│   └── sample_law.txt
│
├── notebooks/                   # experiment & debug notebooks
│
├── tests/                       # pytest (311 cases; run from root: python -m pytest tests/ -q)
│   ├── test_engineering.py      # config / imports / engineering constraints
│   ├── test_smoke.py            # smoke (graph topology / tool registry assertions)
│   ├── test_plan_parse.py       # planner JSON parsing / malformed-output repair / fail-loud
│   ├── test_boundary_*.py       # boundary cases (high/medium/low backend)
│   ├── test_fusion.py           # RRF fusion pure functions (8 cases)
│   ├── test_rrf_retrieval.py   # dual-route recall / orchestration / tool-surface contracts (10 cases)
│   ├── test_eval_metrics.py     # metrics + threshold-calibration helpers
│   ├── test_p0_retrieval.py     # backend abstraction / state score semantics
│   ├── test_hitl_single_question.py # HITL intake payload / resume normalization
│   ├── test_dialogue_events.py  # dialogue-log event stream + aggregation (real PG)
│   ├── test_docx_generation.py  # Word document full chain (template/tool/graph/API, 28 cases)
│   ├── test_tracing.py / test_trace_db.py / test_trace_e2e.py # LLM tracing
│   ├── test_mcp.py              # MCP mounting & stdio end-to-end
│   └── ...
│
├── scripts/
│   ├── run_nb.py                # batch notebook runner
│   ├── ingest_cases_pgvector.py # case corpus chunking + BGE embedding + pgvector ingestion (idempotent)
│   ├── gen_golden_set.py        # retrieval eval golden-set generation (Pinecone pull + stratified sampling + LLM question distillation)
│   ├── run_eval.py              # eval runner: --suite retrieval (metric regression gate) / calibration (tier-threshold calibration)
│   ├── build_docx_template.py   # Word template rebuild (source.docx → template.docx)
│   └── run_pytest_timeout_skip.py # PG-down batch runner with timeout-skip
│
├── frontend/                    # Vue 3 + Vite + Tailwind v4 single-page app (dual-mode chat)
│   ├── src/
│   │   ├── App.vue              # single-page assembly (mode switching / SSE dispatch / HITL / docx modals)
│   │   ├── api.js               # axios REST wrapper
│   │   ├── sse.js               # fetch-based SSE frame parser
│   │   ├── store.js             # reactive global state + typewriter composable
│   │   └── components/          # 18 business components (ChatView / InterruptPanel / SessionDrawer
│   │                            #   / MarkdownView / DocxGenModal / DocxDoneToast etc.)
│   └── vite.config.js           # /api proxy → 127.0.0.1:8000
│
└── docs/
    ├── PROJECT_OVERVIEW.md      # design doc (§1-§15)
    └── superpowers/             # archived design docs & plans
```

Runtime artifacts (`logs/`, `__pycache__/`, `.pytest_cache/`) are not committed.

---

## Tech Stack

| Layer | Technology | Notes |
|------|------|------|
| Agent framework | LangGraph | StateGraph orchestration + PostgreSQL checkpoints |
| LLM | DeepSeek (langchain-openai) | Pro planning / Flash execution, dual-model |
| Web service | FastAPI + Uvicorn | REST API + SSE streaming |
| Data models | Pydantic v2 | end-to-end type constraints |
| Vector search | Pinecone Serverless | dense + sparse independent recall, client-side RRF fusion (k=60) |
| Embedding & rerank | BGE-large-zh-v1.5 / BGE-reranker-large | HuggingFace + sentence-transformers |
| Sparse encoding | pinecone-text BM25Encoder | keyword matching |
| Frontend | Vue 3 + Vite + Tailwind v4 | single-page dual mode, axios + fetch SSE |
| Long-term memory | PostgreSQL 15 + pgvector | semantic memory |
| Web search | SerpAPI | structured Google results |
| PDF generation | markdown + pdfkit (wkhtmltopdf) | Markdown → HTML → PDF |
| Word documents | docxtpl + YAML templates | Jinja2-tag injection, dynamic Pydantic schema extraction |
| Tool extension | MCP (stdio/http) | law-search server dual transport, graceful offline degradation |
| Text splitting | langchain-text-splitters | MarkdownHeader + RecursiveChar |
| Logging | Python logging + contextvars | 5 structured loggers + session propagation |
| Tracing | traced decorator + PostgreSQL trace tables + LangSmith | full-chain runs/spans observability |

---

<div align="center">
  <sub>Built with LangGraph · DeepSeek · Pinecone · FastAPI · Vue3</sub>
</div>
