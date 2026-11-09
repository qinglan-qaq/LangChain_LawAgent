# 统一审计监控页 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 monitor 页以一份 `AuditView` 同时回答“证据链是否成立、上下文是否正确、性能与异常在哪”，并保留按计划步骤折叠的时间轴。

**Architecture:** 事实层仍是 `trace_runs/stage_chain/minor_states/trace_spans`。新增只读 builder 把这些行确定性地派生为 `AuditView`；新端点 `/monitor/runs/{id}/audit-view` 与 `/monitor/stages/{id}/audit-detail` 只消费该模型。埋点只往现有 JSONB 增键（`output_json/context_meta/refs/metrics`），不建表、不改 Agent 图语义。前端五段式页面消费同一模型，原始 JSON 只在抽屉“原始数据”tab 按需请求。

**Tech Stack:** FastAPI + Pydantic v2 + psycopg3 + PostgreSQL JSONB；Vue 3 + Vite 8 + Tailwind 4；pytest + TestClient + 真实 PG（不可用显式 skip）。

**Spec:** `docs/superpowers/specs/2026-11-08-monitor-unified-audit-design.md`

## Global Constraints

- 不新增数据库表、不做 DDL；所有新字段只写入现有 JSONB。
- 不改 Agent 图语义；`audit_view.py` 只读，不参与决策。
- 旧 run 缺采集字段时标 `unknown/partial`，禁止伪造完整。
- `audit-view` 不返回 prompt、完整工具参数、`AgentState`；仅 `audit-detail?include_raw=1` 返回。
- 响应目标 < 200 KB：`documents` ≤ 50、`tool_calls` ≤ 200、`timeline` 摘要优先。
- 保留全部现有 `/monitor/*` 端点，行为不变。
- 后端测试若 PG 不可用必须 `pytest.skip("PG 不可用, 显式跳过(不 mock)")`。
- 用户未要求提交：各任务最后的 commit 步骤执行时跳过，只保留文件改动。
- 代码/注释用中文，风格跟随现有文件；不要引入新依赖。

---

## File Structure

- Create `lawApp_LangGraph/FastAPI/audit_model.py` — 审计视图 Pydantic 契约。
- Create `lawApp_LangGraph/audit_view.py` — 纯函数 builder：分组、证据、上下文、性能、anomaly、verdict。
- Create `lawApp_LangGraph/evidence.py` — 文档规范化与稳定 `evidence_id`。
- Modify `lawApp_LangGraph/FastAPI/api.py` — 两个新端点 + 行加载 helper。
- Modify `lawApp_LangGraph/minor_state.py` — 输出投影补证据/引用/决策摘要。
- Modify `lawApp_LangGraph/tracing.py` — refs 写证据 ID、context_meta 映射、tokens/metrics。
- Modify `lawApp_LangGraph/context.py` — context_meta 补 `history.turns` 与 `memory` 段。
- Modify `lawApp_LangGraph/config.py`、`lawApp_LangGraph/FastAPI/model.py` — 慢节点阈值下发。
- Create `tests/test_audit_view.py` — builder 单测（纯函数 + seeded PG）。
- Create `tests/test_audit_api.py` — 端点契约（真实 PG）。
- Create `frontend/src/components/audit/*.vue` — 五段式与抽屉。
- Modify `frontend/src/api.js`、`frontend/src/views/MonitorView.vue` — 接入 audit-view、深链、轮询。

---

### Task 1: 审计模型契约与夹具

**Files:**
- Create: `lawApp_LangGraph/FastAPI/audit_model.py`
- Create: `tests/test_audit_view.py`

**Interfaces:**
- Produces:
  - `AuditView(schema_version:int=1, generated_at:str, degraded:bool=False, run:RunHeader, chain:list[ChainRun], verdicts:AuditVerdicts, timeline:list[TimelineGroup], evidence:EvidenceBoard, context:ContextMatrix, performance:PerformanceBoard, anomalies:list[Anomaly])`
  - `AuditNodeDetail(node:AuditNode, decision:DecisionDetail, evidence:EvidenceBoard, context:ContextNode, calls:list[CallItem], raw:Optional[RawBlock]=None)`
  - `VerdictLevel = Literal["good","warn","bad","unknown","na"]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_audit_view.py
from lawApp_LangGraph.FastAPI.audit_model import AuditVerdicts, AuditView, RunHeader


def test_audit_view_minimal_serializes_with_schema_version():
    view = AuditView(
        generated_at="2026-11-08T10:00:00+00:00",
        run=RunHeader(run_id="r1", session_id="s1"),
        chain=[],
        verdicts=AuditVerdicts(),
        timeline=[],
        evidence={"tool_calls": [], "documents": [], "citations": [], "gaps": []},
        context={"segments": [], "nodes": [], "summary": {}},
        performance={},
        anomalies=[],
    )
    body = view.model_dump()
    assert body["schema_version"] == 1
    assert body["run"]["run_id"] == "r1"
    assert body["verdicts"]["flow"]["level"] == "unknown"


def test_evidence_board_defaults_are_empty_lists():
    from lawApp_LangGraph.FastAPI.audit_model import EvidenceBoard

    board = EvidenceBoard()
    assert board.tool_calls == [] and board.documents == [] and board.gaps == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_audit_view.py::test_audit_view_minimal_serializes_with_schema_version -v`
Expected: FAIL `ModuleNotFoundError: No module named 'lawApp_LangGraph.FastAPI.audit_model'`

- [ ] **Step 3: Write minimal implementation**

`audit_model.py` 全部模型按 spec §统一审计模型落地。关键字段：

```python
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

VerdictLevel = Literal["good", "warn", "bad", "unknown", "na"]
GroupKind = Literal["decision", "step", "hitl", "answer"]


class JumpTarget(BaseModel):
    section: str = "timeline"
    stage_id: Optional[int] = None
    evidence_id: Optional[str] = None


class Verdict(BaseModel):
    key: str
    level: VerdictLevel = "unknown"
    summary: str = ""
    reasons: List[str] = Field(default_factory=list)
    jump: JumpTarget = Field(default_factory=JumpTarget)


class AuditVerdicts(BaseModel):
    flow: Verdict = Field(default_factory=lambda: Verdict(key="flow"))
    evidence: Verdict = Field(default_factory=lambda: Verdict(key="evidence"))
    context: Verdict = Field(default_factory=lambda: Verdict(key="context"))
    performance: Verdict = Field(default_factory=lambda: Verdict(key="performance"))
```

其余 `RunHeader/ChainRun/TimelineGroup/AuditNode/EvidenceBoard/ToolCallEvidence/DocumentEvidence/CitationEvidence/EvidenceGap/ContextMatrix/ContextNode/PerformanceBoard/PerformanceSlow/PerformanceError/TokenBoard/Anomaly/AuditView/AuditNodeDetail/DecisionDetail/CallItem/RawBlock` 同文件定义，字段名必须与 spec 完全一致；所有可选关联字段用 `Optional[int] = None`，列表用 `Field(default_factory=list)`，字典用 `Field(default_factory=dict)`。`AuditVerdicts` 四张卡默认 `level="unknown"`，避免旧 run 被误判 good。

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_audit_view.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: Commit（按 AGENTS.md 跳过）**

---

### Task 2: 计划轮次与时间轴分组

**Files:**
- Create: `lawApp_LangGraph/audit_view.py`
- Test: `tests/test_audit_view.py`

**Interfaces:**
- Produces:
  - `group_timeline(stages: list[dict], minors_by_stage: dict[int, dict]) -> list[TimelineGroup]`
  - 每个 stage dict 至少有 `stage_id,run_id,node_name,seq,status,started_at,ended_at,latency_ms`
  - 每个 minor dict 至少有 `attempt,phase,output_json,refs,error,context_meta`

- [ ] **Step 1: Write the failing test**

```python
def test_group_timeline_folds_executor_tools_merge_by_plan_step():
    from lawApp_LangGraph.audit_view import group_timeline

    stages = [
        _stage(1, "r1", "ingest", 1),
        _stage(2, "r1", "planner", 1, out={"step_count": 2}),
        _stage(3, "r1", "executor", 2, out={"step_index": 0, "tool": "fetch_laws"}),
        _stage(4, "r1", "tools", 1, out={"tool": "fetch_laws", "law_count": 3}),
        _stage(5, "r1", "merge", 1, out={"step_index": 0}),
        _stage(6, "r1", "executor", 2, out={"step_index": 1, "tool": "search_memory"}),
        _stage(7, "r1", "tools", 1, out={"tool": "search_memory"}),
        _stage(8, "r1", "merge", 1, out={"step_index": 1}),
        _stage(9, "r1", "finalize", 1, out={"answer_len": 10}),
    ]
    groups = group_timeline(stages, {})
    kinds = [g.kind for g in groups]
    assert kinds == ["decision", "decision", "step", "step", "answer"]
    assert groups[0].node_names == ["ingest"]
    assert groups[1].node_names == ["planner"]
    assert groups[2].node_names == ["executor", "tools", "merge"]
    assert groups[2].plan_round == 1 and groups[2].step_index == 0
    assert groups[3].step_index == 1
    assert len(groups) <= 12


def _stage(sid, rid, name, seq, out=None):
    from datetime import datetime, timezone

    return {
        "stage_id": sid, "run_id": rid, "node_name": name, "seq": seq,
        "status": "ok", "started_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "ended_at": datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
        "latency_ms": 1000,
        "output_json": out or {},
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_audit_view.py::test_group_timeline_folds_executor_tools_merge_by_plan_step -v`
Expected: FAIL `ImportError: cannot import name 'group_timeline'`

- [ ] **Step 3: Write minimal implementation**

在 `audit_view.py` 定义常量与派生：

```python
HITL_NODES = {
    "ask_element", "field_clarify", "risk_confirm", "docx_confirm",
    "mid_clarify", "degrade_confirm", "budget_confirm",
    "hitl_degrade", "hitl_budget", "element_fill",
}
DECISION_NODES = {"element_assess", "planner", "replan_check", "replanner"}
ANSWER_NODES = {"finalize", "chitchat"}
STEP_NODES = {"executor", "tools", "merge"}


def _node_kind(name: str) -> str:
    if name in DECISION_NODES:
        return "decision"
    if name in HITL_NODES:
        return "hitl"
    if name in ANSWER_NODES:
        return "answer"
    if name in STEP_NODES:
        return "step"
    return "decision"
```

`group_timeline` 按 `(started_at, seq)` 排序，跨 run 链维护 `plan_round`：遇到 `planner` 或 `replanner` 先自增（首个 planner 为 1）；`executor` 从 `output_json.step_index` 取步骤，`tools/merge` 继承最近 executor 的 `step_index`；同一 run + plan_round + step_index 的 executor/tools/merge 合并为一组。decision/hitl/answer 每个节点一组。组 `group_id` 用 `f"{run_id}:{kind}:{first_stage_id}"`，`status` 按 `error > interrupted > running > ok` 聚合。

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_audit_view.py -v`
Expected: PASS

- [ ] **Step 5: Commit（按 AGENTS.md 跳过）**

---

### Task 3: Verdicts、Evidence、Context、Performance、Anomaly

**Files:**
- Modify: `lawApp_LangGraph/audit_view.py`
- Test: `tests/test_audit_view.py`

**Interfaces:**
- Produces:
  - `derive_verdicts(run: dict, stages: list[dict], evidence: EvidenceBoard, context: ContextMatrix, performance: PerformanceBoard, anomalies: list[dict]) -> AuditVerdicts`
  - `build_evidence(run: dict, stages: list[dict], minors: list[dict], spans: list[dict]) -> EvidenceBoard`
  - `build_context(stages: list[dict], minors: list[dict]) -> ContextMatrix`
  - `build_performance(run: dict, chain: list[dict], stages: list[dict], minors: list[dict], spans: list[dict], slow_ms: int) -> PerformanceBoard`
  - `collect_anomalies(...) -> list[Anomaly]`

- [ ] **Step 1: Write the failing test**

```python
def test_evidence_maps_tool_span_to_document_and_citation():
    from lawApp_LangGraph.audit_view import build_evidence

    run = {"run_id": "r1", "mode": "attorney"}
    stages = [
        _stage(1, "r1", "tools", 1, out={"tool": "retrieve_legal_knowledge"}),
        _stage(2, "r1", "finalize", 1, out={
            "citations": [{"label": "（2023）京01民终1号", "type": "case",
                           "evidence_ids": ["abc"], "resolved": True}]}),
    ]
    spans = [{
        "span_id": 11, "run_id": "r1", "span_type": "tool",
        "name": "retrieve_legal_knowledge", "status": "ok", "latency_ms": 88,
        "input": {"query": "离婚"}, "output": {
            "rag_documents": [{"case_number": "（2023）京01民终1号",
                               "relevance_score": 0.8, "chunk_text": "..."}],
            "evaluation": {"quality_verdict": "相关"},
        },
        "token_usage": None, "started_at": None,
    }]
    board = build_evidence(run, stages, [], spans)
    assert len(board.tool_calls) == 1
    assert len(board.documents) == 1
    assert board.documents[0].kind == "case"
    assert len(board.citations) == 1 and board.citations[0].resolved is True


def test_context_inherits_missing_meta_and_flags_truncation():
    from lawApp_LangGraph.audit_view import build_context

    stages = [_stage(1, "r1", "planner", 1), _stage(2, "r1", "executor", 1)]
    minors = [
        {"stage_id": 1, "context_meta": {"current_query": {"chars": 10, "hash": "a"},
                                          "truncated": True}},
        {"stage_id": 2, "context_meta": {}},
    ]
    matrix = build_context(stages, minors)
    assert matrix.nodes[0].segments["current_query"].present is True
    assert matrix.nodes[1].inherited_from_stage_id == 1
    assert matrix.nodes[1].segments["current_query"].injected is False
    assert matrix.summary["truncated_segments"] == ["current_query"]


def test_verdicts_flag_unresolved_citation_as_warn():
    from lawApp_LangGraph.FastAPI.audit_model import (
        AuditVerdicts, ContextMatrix, EvidenceBoard, EvidenceGap,
        CitationEvidence, PerformanceBoard,
    )
    from lawApp_LangGraph.audit_view import derive_verdicts

    ev = EvidenceBoard(citations=[CitationEvidence(
        label="某案", type="case", evidence_ids=[], resolved=False)])
    ctx = ContextMatrix(segments=["current_query"], nodes=[], summary={})
    perf = PerformanceBoard(total_ms=100, execution_ms=90, tokens={"total": 1})
    verdicts = derive_verdicts({"run_id": "r1", "status": "ok", "mode": "attorney"},
                               [], ev, ctx, perf, [])
    assert verdicts.evidence.level == "warn"
    assert any("未解析" in r for r in verdicts.evidence.reasons)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_audit_view.py -v`
Expected: FAIL `ImportError: cannot import name 'build_evidence'`

- [ ] **Step 3: Write minimal implementation**

`audit_view.py` 增加纯函数：

1. 文档规范化（`evidence.py`）：
   - `normalize_document(kind, raw) -> dict` 输出 `{kind,title,score,verdict,doc_key}`
   - `evidence_id_for(run_id, kind, doc_key) -> sha1(f"{run_id}:{kind}:{doc_key}")[:16]`
   - `doc_key`：法条 `law_title|article_number`；案例 `case_number` 或 `title` 或 `sha1(chunk_text)`；记忆 `sha1(content)`；网络 `title|link`。
2. `build_evidence`：
   - 遍历 `spans` 中 `span_type=="tool"`，解析 output 字典（字符串先 `json.loads`），提取 `rag_documents/law_results/memory_results/web_search_results`。
   - 文档 `source_span_id=span_id`；`source_stage_id` 用同 run 时间区间包含 span 的 `tools` 节点，找不到则 `None`。
   - `used_in_analysis`：若任意 minor `refs.evidence_ids_used` 包含该 id 为 True；若存在 `evidence_ids_used` 字段但不含为 False；否则 null。
   - `used_in_answer`：引用列表的 `evidence_ids` 含该 id 为 True；若引用存在但无 id 为 null。
   - citations 优先取 `finalize/chitchat` 的 `output_json.citations`；缺失时 `resolved=False` 并生成 `cited_without_source` gap（仅当 run 有 `final_answer`）。
   - `retrieved_not_used` 只为 `used_in_analysis is False` 的文档生成。
3. `build_context`：
   - 规范段 `["task","current_query","critical_facts","history","evidence","constraints","memory"]`。
   - 有 `context_meta` 的节点：每段 `{present,chars,hash,truncated,injected=True}`；顶层 `truncated=true` 时把所有 present 段标截断（并写入 summary）。
   - 无 meta 节点：继承最近一个有 meta 节点的段，`injected=False`，`inherited_from_stage_id=最近 stage_id`，`diff={added:[],removed:[]}`。
   - 有 meta 节点 diff 与上一个有 meta 节点比 hash：新增/变更进 `added`，消失进 `removed`。
   - summary：`total_chars` 只累计 `injected=True` 的段；`history_turns=meta.history.turns`（缺省 null）；`memory_hits=meta.memory.chars>0` 时取 `meta.memory.hits`，否则 0。
4. `build_performance`：`total_ms` 用链首 start→链尾 end（running 用 now）；`execution_ms=sum(stage.latency_ms)`；`wait_human_ms=sum(run 间空档)`；`overhead_ms=max(0,total-exec-wait)`；`slowest` 取 top3；`errors` 收集 `status=="error"` 或 `minor.error`；`tokens` 从 minor.metrics.tokens 汇总，缺失再从 span.token_usage（同 run+name 映射到 stage）补；`retries=sum(attempt>1)`；`limit_hit=bool(run.metrics.limit_hit)`。
5. `collect_anomalies`：逐条生成 spec 固定 code；`minor_missing` 每个缺行 stage 一条（上限 20），`span_missing` 整 run 一条；旧 run 无 evidence id / context_meta / citations 时追加一条 `audit_partial`。
6. `derive_verdicts`：严格按 spec §AuditVerdicts 的 level 规则；`flow` 对 `interrupted` 不判 bad（标 good + reason “等待人工”），有 replan/retry 时 warn，`status in ("error","cancelled")` bad；evidence 在 `mode=="assistant"` 且无检索时 na；旧 run 无关联字段 unknown；context 无任何 meta 时 unknown；performance 有 error/limit_hit bad，慢点或 retry warn。

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_audit_view.py -v`
Expected: PASS

- [ ] **Step 5: Commit（按 AGENTS.md 跳过）**

---

### Task 4: API 端点与契约测试

**Files:**
- Modify: `lawApp_LangGraph/FastAPI/api.py`
- Create: `tests/test_audit_api.py`

**Interfaces:**
- Consumes:
  - `build_audit_view(run_id: str, chain: bool, runs: list[dict], stages: list[dict], minors: list[dict], spans: list[dict], slow_ms: int) -> AuditView`
  - `build_audit_detail(view: AuditView, stage_id: int, stages: list[dict], minors: list[dict], spans: list[dict], include_raw: bool) -> AuditNodeDetail`
- Produces:
  - `GET /monitor/runs/{run_id}/audit-view?chain=1&after_stage_id=0` → `AuditView`
  - `GET /monitor/stages/{stage_id}/audit-detail?include_raw=0` → `AuditNodeDetail`

- [ ] **Step 1: Write the failing API test**

```python
# tests/test_audit_api.py（沿用 test_monitor_api_v2 的 _pg_ok/_seed 模式）
def test_audit_view_returns_schema_and_chain():
    ids = _seed_audit()
    try:
        with _client() as c:
            r = c.get(f"/monitor/runs/{ids['child']}/audit-view?chain=1")
            assert r.status_code == 200
            body = r.json()
            assert body["schema_version"] == 1
            assert body["run"]["run_id"] == ids["child"]
            assert len(body["chain"]) == 2
            assert body["verdicts"]["flow"]["level"] in {
                "good", "warn", "bad", "unknown", "na"}
            assert {"tool_calls", "documents", "citations", "gaps"} <= set(
                body["evidence"])
            assert "timeline" in body and "performance" in body
    finally:
        _cleanup()


def test_audit_detail_hides_raw_by_default():
    ids = _seed_audit()
    try:
        with _client() as c:
            body = c.get(
                f"/monitor/stages/{ids['stage_fin']}/audit-detail").json()
            assert "raw" not in body or body["raw"] is None
            assert "final_prompts" not in json.dumps(body)
            raw = c.get(
                f"/monitor/stages/{ids['stage_fin']}/audit-detail?include_raw=1"
            ).json()
            assert raw["raw"]["minor_state"]["node_name"] == "finalize"
    finally:
        _cleanup()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_audit_api.py -v`
Expected: FAIL 404（端点未注册）

- [ ] **Step 3: Write minimal implementation**

`api.py` 增加：

```python
async def _load_audit_rows(conn, run_id: str, chain: bool):
    runs, rows = await _load_chain(conn, run_id, chain)
    if not runs:
        raise HTTPException(404, f"run {run_id} 不存在")
    minors = {row[0]: {
        "stage_id": row[0], "attempt": row[8], "phase": row[9],
        "input_text": row[10], "output_text": row[11],
        "output_json": _json_obj(row[12]), "context_meta": _json_obj(row[13]),
        "metrics": _json_obj(row[14]), "route": row[15],
        "route_reason": row[16], "error": row[17], "refs": _json_obj(row[18]),
        "parent_stage_id": row[19], "content_source": row[20],
    } for row in rows}
    cur = await conn.execute(
        "SELECT id, run_id, span_type, name, status, input, output, state, "
        "latency_ms, token_usage, started_at FROM trace_spans "
        "WHERE run_id = ANY(%s) ORDER BY started_at", ([r["run_id"] for r in runs],))
    ...
```

`audit-view`：404/503 与现有端点一致；`after_stage_id>0` 时只保留 `any(sid > after for sid in group.stage_ids)` 的 timeline 组，其余段仍返回完整快照。`audit-detail`：先按 stage 找 run，再加载该 run（chain=False）数据，调用 builder；`include_raw=1` 才填 `RawBlock(minor_state=..., span_input=..., span_output=..., span_state=...)`。

`monitor_config` 返回新增 `slow_node_ms=settings.monitor_slow_node_ms`（`MonitorConfig` 加默认字段，保持旧响应兼容）。

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_audit_api.py tests/test_monitor_api_v2.py -v`
Expected: PASS

- [ ] **Step 5: Commit（按 AGENTS.md 跳过）**

---

### Task 5: 证据 ID 与引用关联埋点

**Files:**
- Create: `lawApp_LangGraph/evidence.py`
- Modify: `lawApp_LangGraph/minor_state.py`
- Modify: `lawApp_LangGraph/tracing.py`
- Test: `tests/test_audit_view.py`

**Interfaces:**
- Produces:
  - `collect_documents(tool_name: str, result: Any) -> list[dict]`
  - `evidence_id_for(run_id: str, kind: str, doc_key: str) -> str`
  - `project_output` 内部对 `tools` 写隐藏键 `_evidence_docs`/`_evidence_used_docs`，对 `finalize/chitchat` 写 `_citation_docs`
  - `refs.evidence_ids`、`refs.evidence_ids_used`、`refs.citation_evidence_ids`

- [ ] **Step 1: Write the failing test**

```python
def test_evidence_id_is_stable_and_kind_scoped():
    from lawApp_LangGraph.evidence import evidence_id_for

    a = evidence_id_for("r1", "case", "（2023）京01民终1号")
    b = evidence_id_for("r1", "case", "（2023）京01民终1号")
    c = evidence_id_for("r1", "law", "（2023）京01民终1号")
    assert a == b and len(a) == 16
    assert a != c
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_audit_view.py::test_evidence_id_is_stable_and_kind_scoped -v`
Expected: FAIL `ModuleNotFoundError: lawApp_LangGraph.evidence`

- [ ] **Step 3: Write minimal implementation**

`evidence.py`：

```python
import hashlib
import json
from typing import Any


def _canon(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def doc_key(kind: str, doc: dict) -> str:
    if kind == "law":
        return f"{doc.get('law_title', '')}|{doc.get('article_number', '')}"
    if kind == "case":
        return str(doc.get("case_number") or doc.get("title")
                   or hashlib.sha1(str(doc.get("chunk_text") or "").encode()).hexdigest()[:12])
    if kind == "memory":
        return hashlib.sha1(str(doc.get("content") or "").encode()).hexdigest()[:16]
    return f"{doc.get('title', '')}|{doc.get('link', '')}"


def evidence_id_for(run_id: str, kind: str, key: str) -> str:
    return hashlib.sha1(f"{run_id}:{kind}:{key}".encode("utf-8")).hexdigest()[:16]


def collect_documents(tool_name: str, result: Any) -> list[dict]:
    data = result if isinstance(result, dict) else {}
    out = []
    for kind, field in (("case", "rag_documents"), ("law", "law_results"),
                        ("memory", "memory_results"), ("web", "web_search_results")):
        for doc in data.get(field) or []:
            if isinstance(doc, dict):
                out.append({"kind": kind, "doc": doc, "doc_key": doc_key(kind, doc)})
    return out
```

`minor_state._output_parts` 的 `tools` 分支：

```python
from lawApp_LangGraph.evidence import collect_documents
...
docs_all = collect_documents(out.get("tool") or "", parsed)
out["_evidence_docs"] = docs_all[:60]
if parsed.get("prompts_record"):
    used = collect_documents("analyze_legal_issue", parsed["prompts_record"])
    out["_evidence_used_docs"] = used[:60]
out["_evidence_counts"] = {
    "laws": len(laws), "cases": len(docs), "memory": len(parsed.get("memory_results") or []),
    "web": len(web),
}
```

`finalize/chitchat` 分支（`_output_parts`）写 `_citation_docs`：

```python
cites = []
for law in (_get(state, "law_results") or []):
    d = _as_dict(law)
    cites.append({"label": f"{d.get('law_title','')} {d.get('article_number','')}".strip(),
                  "type": "law", "kind": "law",
                  "doc_key": doc_key("law", d)})
for doc in (_get(state, "rag_documents") or []):
    d = _as_dict(doc)
    cites.append({"label": (d.get("case_number") or d.get("case_cause") or ""),
                  "type": "case", "kind": "case",
                  "doc_key": doc_key("case", d)})
out["_citation_docs"] = [c for c in cites if c["label"]][:50]
```

`tracing._close_minor` 在 `out` 处理处：

```python
ev_docs = out.pop("_evidence_docs", []) if isinstance(out, dict) else []
ev_used = out.pop("_evidence_used_docs", []) if isinstance(out, dict) else []
cit_docs = out.pop("_citation_docs", []) if isinstance(out, dict) else []
if ev_docs:
    refs["evidence_ids"] = [evidence_id_for(run.run_id, d["kind"], d["doc_key"])
                            for d in ev_docs]
if ev_used:
    refs["evidence_ids_used"] = [evidence_id_for(run.run_id, d["kind"], d["doc_key"])
                                 for d in ev_used]
if cit_docs:
    out["citations"] = [{
        "label": c["label"], "type": c["type"],
        "evidence_ids": [evidence_id_for(run.run_id, c["kind"], c["doc_key"])],
        "resolved": True,
    } for c in cit_docs]
    refs["citation_evidence_ids"] = sorted({i for c in out["citations"]
                                           for i in c["evidence_ids"]})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_audit_view.py tests/test_minor_state.py -v`
Expected: PASS

- [ ] **Step 5: Commit（按 AGENTS.md 跳过）**

---

### Task 6: 决策摘要、上下文覆盖、tokens

**Files:**
- Modify: `lawApp_LangGraph/minor_state.py`
- Modify: `lawApp_LangGraph/tracing.py`
- Modify: `lawApp_LangGraph/context.py`
- Test: `tests/test_audit_view.py`

**Interfaces:**
- Produces:
  - `output_json.decision = {"summary": str, "reason": str}`
  - `context_meta.history.turns: int`、`context_meta.memory = {"chars": int, "hits": int, "hash": str}`
  - `metrics.tokens = {"prompt": int, "completion": int, "total": int}`、`metrics.tool_ms`、`metrics.llm_ms`

- [ ] **Step 1: Write the failing test**

```python
def test_decision_summary_present_for_planner_and_executor():
    from lawApp_LangGraph.minor_state import project_output

    state = {"query": "q", "plan": [{"step_id": 1, "tool_name": "fetch_laws",
                                     "description": "查法条"}]}
    _, planner = project_output("planner", {"plan": state["plan"]}, state)
    _, executor = project_output("executor", {
        "messages": [{"tool_calls": [{"name": "fetch_laws", "args": {"query": "q"}}]}]},
        state)
    assert planner["decision"]["summary"]
    assert executor["decision"]["summary"].startswith("调用")


def test_context_meta_history_turns_and_memory():
    from lawApp_LangGraph.context import build_context

    state = {
        "query": "离婚", "mode": "attorney",
        "messages": [{"type": "human", "content": "在吗"},
                     {"type": "ai", "content": "在"},
                     {"type": "human", "content": "离婚"}],
        "prompts_record": {"memory_results": [{"content": "旧结论"}]},
    }
    _, meta = build_context(state, "planner")
    assert meta["history"]["turns"] == 2
    assert meta["memory"]["hits"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_audit_view.py -v`
Expected: FAIL `KeyError: 'decision'`

- [ ] **Step 3: Write minimal implementation**

1. `minor_state._output_parts` 每个节点分支补 `out["decision"]`：
   - planner：`{"summary": f"计划 {n} 步", "reason": ", ".join(tools)}`
   - executor：`{"summary": f"调用 {tool}", "reason": step.description}` / `"推进步骤 {idx+1}"`
   - replan_check：`{"summary": "需要重规划" if replan_needed else "无需重规划", "reason": insufficient_reason}`
   - replanner：`{"summary": f"补充 {n} 步", "reason": "证据不足"}`
   - element_assess：`{"summary": f"待补 {n} 项" / "要素齐全", "reason": question_category}`
   - finalize/chitchat：`{"summary": f"生成回答 {len(answer)} 字", "reason": branch}`
   - tools/merge：沿用 `out` 的 `tool`/`step_index` 生成 summary。
2. `context.build_context`：
   - `hist = _history(state)` 后：`meta["history"] = {**meta["history"], "turns": sum(1 for m in _get(state,"messages") if role=="human")}`；若 `recent` 为空则用 `len(recent)//2`。
   - 记忆：`mem_results = _as_dict(_get(state, "prompts_record")).get("memory_results") or _get(state, "memory_results") or []`；非空时 `meta["memory"]={"chars": _chars(mem_results), "hits": len(mem_results), "hash": sha1(...)[:12]}`。
3. `tracing._close_minor` 增 `span` 参数（两处调用都传 `span=span`）；计算子 span：

```python
children = [s for s in run.spans
            if s is not span and s.started_at and s.started_at >= span.started_at]
tokens = {
    "prompt": sum((s.token_usage or {}).get("prompt") or 0 for s in children),
    "completion": sum((s.token_usage or {}).get("completion") or 0 for s in children),
}
tokens["total"] = tokens["prompt"] + tokens["completion"]
metrics = {
    "tokens": tokens,
    "tool_ms": sum(s.latency_ms for s in children if s.span_type == "tool"),
    "llm_ms": sum(s.latency_ms for s in children if s.span_type == "llm"),
    "child_spans": len(children),
}
```

`enqueue_close(..., metrics=metrics)`。

4. `_CONTEXT_PURPOSE` 修正 `finalize`：`_minor_context_meta` 增加 `state` 参数或特判 `node_name=="finalize"` 时按 `mode` 取 `finalize_attorney/finalize_assistant`；`replanner→planner`、`element_fill/mid_clarify→element_assess`、`hitl_degrade/hitl_budget→replan_check`。`_open_minor`/`_close_minor` 把 `state` 传给 `_minor_context_meta`。

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_audit_view.py tests/test_context.py tests/test_tracing_minor.py -v`
Expected: PASS

- [ ] **Step 5: Commit（按 AGENTS.md 跳过）**

---

### Task 7: 前端审计组件与 API

**Files:**
- Modify: `frontend/src/api.js`
- Create: `frontend/src/components/audit/AuditRunHeader.vue`
- Create: `frontend/src/components/audit/AuditVerdictBar.vue`
- Create: `frontend/src/components/audit/AuditTimeline.vue`
- Create: `frontend/src/components/audit/AuditEvidenceBoard.vue`
- Create: `frontend/src/components/audit/AuditContextMatrix.vue`
- Create: `frontend/src/components/audit/AuditPerformancePanel.vue`
- Create: `frontend/src/components/audit/AuditNodeDrawer.vue`
- Create: `frontend/src/components/audit/auditUi.js`

**Interfaces:**
- Produces:
  - `getAuditView(runId, {chain=true, afterStageId=0})`
  - `getAuditDetail(stageId, includeRaw=false)`
  - 组件 props 均为 `audit-view` 对应片段，事件 `select(stageId)` / `jump(section)` / `close`

- [ ] **Step 1: Write the failing build check**

Run: `cd frontend && npm run build`
Expected: PASS（基线通过，后续改动若引用未定义组件会失败）

- [ ] **Step 2: Add API client**

```javascript
// 统一审计视图: 一次请求拿结论/时间轴/证据/上下文/性能
export const getAuditView = (runId, { chain = true, afterStageId = 0 } = {}) =>
  http.get(`/monitor/runs/${runId}/audit-view`, {
    params: { chain: chain ? 1 : 0, after_stage_id: afterStageId },
  }).then((r) => r.data)

// 节点审计详情: 默认不含 raw(prompt/完整 state), 原始数据 tab 传 includeRaw=true
export const getAuditDetail = (stageId, includeRaw = false) =>
  http.get(`/monitor/stages/${stageId}/audit-detail`, {
    params: { include_raw: includeRaw ? 1 : 0 },
  }).then((r) => r.data)
```

- [ ] **Step 3: Implement components**

`auditUi.js`：

```javascript
export const LEVEL_UI = {
  good: { label: '通过', cls: 'border-emerald-200 bg-emerald-50 text-emerald-700' },
  warn: { label: '注意', cls: 'border-amber-200 bg-amber-50 text-amber-700' },
  bad: { label: '异常', cls: 'border-red-200 bg-red-50 text-red-700' },
  unknown: { label: '未知', cls: 'border-slate-200 bg-slate-50 text-slate-600' },
  na: { label: '不适用', cls: 'border-slate-200 bg-white text-slate-400' },
}
export const fmtMs = (n) => n == null ? '—' : n < 1000 ? `${n}ms` : n < 60000 ? `${(n/1000).toFixed(1)}s` : `${(n/60000).toFixed(1)}m`
```

每个组件只做展示，不发请求：

- `AuditRunHeader`：run_id/session/mode/status/耗时/answer_len；chain 用横向 run 标签 + `wait_before_ms`。
- `AuditVerdictBar`：四张卡渲染 `verdicts`，点击 emit `jump(verdict.jump.section)`，reasons 以 `·` 连接。
- `AuditTimeline`：按 group 渲染；`decision/hitl/answer` 默认展开节点；`step` 组用 `<details>` 折叠，标题 `步骤 {step_index+1}`，显示节点数/证据数/错误数；节点按钮 emit `select(stage_id)`，展示 `node_name/决策摘要/耗时/attempt`。
- `AuditEvidenceBoard`：三段：工具调用（tool/status/耗时/counts）、文档（kind/title/score/used 图标，emit `locate(stage_id, evidence_id)`）、引用（label/resolved）、gaps 橙色列表。
- `AuditContextMatrix`：表格行=节点，列=segments；单元格显示 `chars`、截断 `!`、继承 `↳`；点击行 emit `select`。
- `AuditPerformancePanel`：4 个总量 + slowest 列表 + errors + tokens + retries/limit_hit。
- `AuditNodeDrawer`：固定右侧 `max-w-2xl`，tabs 概览/决策/证据/上下文/调用/原始数据；raw tab 点击时 emit `load-raw`；原始区 `<pre>` 展示 `detail.raw`，并显示“可能含案情与内部 prompt”提示。

- [ ] **Step 4: Run build**

Run: `cd frontend && npm run build`
Expected: PASS

- [ ] **Step 5: Commit（按 AGENTS.md 跳过）**

---

### Task 8: MonitorView 接入、深链与轮询

**Files:**
- Modify: `frontend/src/views/MonitorView.vue`

**Interfaces:**
- Consumes: Task 7 的 `getAuditView/getAuditDetail` 与组件。
- Produces: `/monitor#run=<run_id>&stage=<stage_id>&tab=<tab>` 深链；`VITE_MONITOR_AUDIT=0` 时回退旧时间轴。

- [ ] **Step 1: Write the failing check**

Run: `cd frontend && npm run build`
Expected: 当前 PASS；接入后若组件/变量拼错则 FAIL。

- [ ] **Step 2: Implement integration**

`MonitorView.vue` 增加：

```javascript
const AUDIT_ON = import.meta.env.VITE_MONITOR_AUDIT !== '0'
const auditView = ref(null)
const auditLoading = ref(false)
const auditDetail = ref(null)
const auditRawLoading = ref(false)
const auditTab = ref('overview')
```

- `openRun`：`AUDIT_ON` 时 `await loadAudit()` 并 `selectedStageId = null`；`#tab` 仍保留。
- `loadAudit(after=0)`：`getAuditView(runId, { chain: true, afterStageId: after })`；失败保留旧快照并置 `monitorError`。
- `selectStage`：`AUDIT_ON` 时 `getAuditDetail(stageId, false)`，设置 `auditTab='overview'`；旧的 `getMonitorStageDetail` 只在 flag 关闭时调用。
- `pollSelected`：`AUDIT_ON` 时每 2s 调 `loadAudit()`，仅当 `auditView.run.status === 'running'` 或链中任一 run running 时继续；终态 `ensureTimer` 停表。
- `syncHash`：增加 `tab` 参数；`applyHash` 读取 `tab` 并传给 `selectStage`。
- 模板：选中 run 后先渲染 `<AuditRunHeader>`、`<AuditVerdictBar>`，再按五段：
  ```html
  <section id="audit-section-timeline"><AuditTimeline ... @select="selectStage" /></section>
  <section id="audit-section-evidence"><AuditEvidenceBoard ... @locate="locateEvidence" /></section>
  <section id="audit-section-context"><AuditContextMatrix ... @select="selectStage" /></section>
  <section id="audit-section-performance"><AuditPerformancePanel ... @select="selectStage" /></section>
  <AuditNodeDrawer v-if="selectedStageId" ... @close="closeStage" @load-raw="loadRaw" />
  ```
  `jump(section)` 用 `document.getElementById(...)?.scrollIntoView`。
  `VITE_MONITOR_AUDIT=0` 时保留原 `MonitorTimeline + MinorStateDetail` 分支。

- [ ] **Step 3: Run build**

Run: `cd frontend && npm run build`
Expected: PASS

- [ ] **Step 4: Commit（按 AGENTS.md 跳过）**

---

### Task 9: 回归、性能与收尾

**Files:**
- Modify: `docs/superpowers/specs/2026-11-08-monitor-unified-audit-design.md`（如实现与 spec 有偏差，回写一行说明）
- Test: `tests/test_audit_view.py`, `tests/test_audit_api.py`, `tests/test_monitor_api.py`, `tests/test_monitor_api_v2.py`

- [ ] **Step 1: Run backend regression**

Run: `.venv/Scripts/python -m pytest tests/test_audit_view.py tests/test_audit_api.py tests/test_monitor_api.py tests/test_monitor_api_v2.py tests/test_minor_state.py tests/test_tracing_minor.py tests/test_context.py -v`
Expected: PASS（PG 不可用的用例 skip，不算失败）

- [ ] **Step 2: Measure real run payload**

Run:
```bash
.venv/Scripts/python - <<'PY'
import asyncio, json, time, urllib.request
from lawApp_LangGraph.db import build_dsn
# 取最近一个 run_id 后请求 audit-view, 打印 bytes 与耗时
PY
```
Expected: `< 200KB`；本地 p95 目标 < 500ms（30 节点量级）。若超限，先降 `documents` 上限或摘要长度，不动表结构。

- [ ] **Step 3: Manual walk-through**

用 `MULTIFIX-1:225123432551`（或最近含 HITL 的 run）打开 `/monitor#run=...`：
- 四张结论卡可见且可跳段；
- 27 stage 折叠后组数 ≤ 12；
- 点节点 → 抽屉五 tab；原始数据 tab 展开前提示；
- 证据链：引用 → 文档 → 工具调用 → 节点可定位；
- 旧 run 显示 `部分可审计/unknown`，不伪造。

- [ ] **Step 4: Commit（按 AGENTS.md 跳过）**

---

## Self-Review

**Spec coverage**
- 统一模型 / 一个 builder / 一套 API → Task 1-4。
- 时间轴按计划步骤分组、plan_round/step_index → Task 2。
- 四张 verdict + 固定 anomaly → Task 3。
- 证据链 ID、used_in_analysis/answer、引用解析 → Task 3 + Task 5。
- ContextMatrix 继承与 diff、history_turns/memory_hits → Task 3 + Task 6。
- PerformanceBoard、tokens、wait_human → Task 3 + Task 6。
- 五段式页面 + 抽屉 + 深链 + 轮询 → Task 7-8。
- 旧端点保留、无 DDL、旧 run partial → Task 4 + Task 9。
- 隐私：`audit-view` 无 raw，`audit-detail` 默认无 raw → Task 4。

**Placeholder scan**
- 无 TBD/TODO；每个测试步骤有可执行断言；实现步骤给出关键代码与函数签名。

**Type consistency**
- `evidence_id_for(run_id, kind, doc_key)` 在 Task 3/5 同名同参。
- `group_timeline(stages, minors_by_stage)`、`build_evidence(run, stages, minors, spans)`、`build_context(stages, minors)`、`build_performance(run, chain, stages, minors, spans, slow_ms)`、`derive_verdicts(run, stages, evidence, context, performance, anomalies)` 在 Task 2-4 一致。
- 前端字段名与 `audit_model.py` / spec 一致：`verdicts.flow/evidence/context/performance`、`timeline[].stage_ids`、`evidence.documents[].evidence_id`。
