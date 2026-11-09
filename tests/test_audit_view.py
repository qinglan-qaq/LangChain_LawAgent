"""统一审计 builder 单测 — 纯函数, 不依赖 PG."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from lawApp_LangGraph.FastAPI.audit_model import (
    AuditVerdicts,
    AuditView,
    EvidenceBoard,
    RunHeader,
)

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _stage(
    sid: int,
    rid: str = "r1",
    name: str = "planner",
    seq: int = 1,
    *,
    out: dict | None = None,
    status: str = "ok",
    offset: int = 0,
) -> dict:
    start = BASE + timedelta(seconds=offset)
    return {
        "stage_id": sid,
        "run_id": rid,
        "node_name": name,
        "seq": seq,
        "status": status,
        "started_at": start,
        "ended_at": start + timedelta(milliseconds=800),
        "latency_ms": 800,
        "output_json": out or {},
    }


def _minor(sid: int, **kwargs) -> dict:
    row = {
        "stage_id": sid,
        "attempt": 1,
        "phase": "done",
        "input_text": "",
        "output_text": "",
        "output_json": {},
        "context_meta": {},
        "metrics": {},
        "route": None,
        "route_reason": None,
        "error": None,
        "refs": {},
        "parent_stage_id": None,
        "content_source": "projected",
    }
    row.update(kwargs)
    return row


def test_audit_view_minimal_serializes_with_schema_version():
    view = AuditView(
        generated_at="2026-11-08T10:00:00+00:00",
        run=RunHeader(run_id="r1", session_id="s1"),
        chain=[],
        verdicts=AuditVerdicts(),
        timeline=[],
        evidence=EvidenceBoard(),
        context={"segments": [], "nodes": [], "summary": {}},
        performance={},
        anomalies=[],
    )
    body = view.model_dump()
    assert body["schema_version"] == 1
    assert body["run"]["run_id"] == "r1"
    assert body["verdicts"]["flow"]["level"] == "unknown"
    assert body["evidence"]["documents"] == []


def test_group_timeline_folds_executor_tools_merge_by_plan_step():
    from lawApp_LangGraph.audit_view import group_timeline

    stages = [
        _stage(1, name="ingest", offset=0),
        _stage(2, name="planner", out={"step_count": 2}, offset=1),
        _stage(3, name="executor", seq=1, out={"step_index": 0, "tool": "fetch_laws"}, offset=2),
        _stage(4, name="tools", out={"tool": "fetch_laws", "law_count": 3}, offset=3),
        _stage(5, name="merge", out={"step_index": 0}, offset=4),
        _stage(6, name="executor", seq=2, out={"step_index": 1, "tool": "search_memory"}, offset=5),
        _stage(7, name="tools", out={"tool": "search_memory"}, offset=6),
        _stage(8, name="merge", out={"step_index": 1}, offset=7),
        _stage(9, name="finalize", out={"answer_len": 10}, offset=8),
    ]
    groups = group_timeline(stages, {})
    assert [g.kind for g in groups] == [
        "decision", "decision", "step", "step", "answer"
    ]
    assert groups[0].node_names == ["ingest"]
    assert groups[1].node_names == ["planner"]
    assert groups[2].node_names == ["executor", "tools", "merge"]
    assert groups[2].plan_round == 1 and groups[2].step_index == 0
    assert groups[3].step_index == 1
    assert len(groups) <= 12


def test_group_timeline_replanner_starts_new_plan_round():
    from lawApp_LangGraph.audit_view import group_timeline

    stages = [
        _stage(1, name="planner", offset=0),
        _stage(2, name="executor", out={"step_index": 0}, offset=1),
        _stage(3, name="replanner", offset=2),
        _stage(4, name="executor", out={"step_index": 0}, offset=3),
    ]
    groups = group_timeline(stages, {})
    steps = [g for g in groups if g.kind == "step"]
    assert len(steps) == 2
    assert [g.plan_round for g in steps] == [1, 2]


def test_evidence_maps_tool_span_to_document_and_citation():
    from lawApp_LangGraph.audit_view import build_evidence

    run = {"run_id": "r1", "mode": "attorney"}
    stages = [
        _stage(1, name="tools", out={"tool": "retrieve_legal_knowledge"}, offset=0),
        _stage(2, name="finalize", offset=1),
    ]
    minors = {
        2: _minor(2, output_json={"citations": [{
            "label": "（2023）京01民终1号", "type": "case",
            "evidence_ids": ["abc"], "resolved": True,
        }]}),
    }
    spans = [{
        "span_id": 11, "run_id": "r1", "span_type": "tool",
        "name": "retrieve_legal_knowledge", "status": "ok", "latency_ms": 88,
        "input": {"query": "离婚"}, "output": {
            "rag_documents": [{
                "case_number": "（2023）京01民终1号",
                "relevance_score": 0.8, "chunk_text": "...",
            }],
            "evaluation": {"quality_verdict": "相关"},
        },
        "token_usage": None, "started_at": BASE,
    }]
    board = build_evidence(run, stages, minors, spans)
    assert len(board.tool_calls) == 1
    assert len(board.documents) == 1
    assert board.documents[0].kind == "case"
    assert board.documents[0].verdict == "相关"
    assert len(board.citations) == 1 and board.citations[0].resolved is True


def test_evidence_flags_unresolved_citation_and_unused_document():
    from lawApp_LangGraph.audit_view import build_evidence

    stages = [
        _stage(1, name="tools", offset=0),
        _stage(2, name="finalize", offset=1),
    ]
    minors = {
        1: _minor(1, refs={"evidence_ids_used": []}),
        2: _minor(2, output_json={"citations": [{
            "label": "某案", "type": "case", "evidence_ids": [], "resolved": False,
        }]}),
    }
    spans = [{
        "span_id": 12, "run_id": "r1", "span_type": "tool", "name": "fetch_laws",
        "status": "ok", "latency_ms": 10, "input": {},
        "output": {"law_results": [{"law_title": "民法典", "article_number": "1041"}]},
        "started_at": BASE,
    }]
    board = build_evidence({"run_id": "r1"}, stages, minors, spans)
    codes = {g.code for g in board.gaps}
    assert "cited_without_source" in codes
    assert "retrieved_not_used" in codes


def test_evidence_maps_analysis_used_docs_from_prompts_record():
    from lawApp_LangGraph.audit_view import _counts_from_output, build_evidence
    from lawApp_LangGraph.evidence import evidence_id_for

    stages = [_stage(1, name="tools", offset=0)]
    pr = {
        "evaluate_retrieved_documents": [{"case_number": "C1"}],
        "laws_results": [{"law_title": "民法典", "article_number": "第1条"}],
        "memory_results": [{"content": "m1"}],
        "web_search_results": [{"title": "t1", "link": "l1"}],
    }
    used_ids = [
        evidence_id_for("r1", "case", "C1"),
        evidence_id_for("r1", "law", "民法典|第1条"),
    ]
    minors = {1: _minor(1, refs={"evidence_ids_used": used_ids})}
    spans = [{
        "span_id": 13, "run_id": "r1", "span_type": "tool",
        "name": "analyze_legal_issue", "status": "ok", "latency_ms": 10,
        "input": {}, "output": {"final_answer": "x", "prompts_record": pr},
        "started_at": BASE,
    }]
    counts = _counts_from_output(spans[0]["output"])
    assert counts == {"laws": 1, "cases": 1, "memory": 1, "web": 1}

    board = build_evidence(
        {"run_id": "r1"}, stages, minors,
        spans + [{
            "span_id": 14, "run_id": "r1", "span_type": "tool",
            "name": "retrieve_legal_knowledge", "status": "ok",
            "latency_ms": 10, "input": {},
            "output": {"rag_documents": [{"case_number": "C1"}]},
            "started_at": BASE,
        }],
    )
    doc = next(d for d in board.documents if d.evidence_id == used_ids[0])
    assert doc.used_in_analysis is True


def test_tool_span_maps_to_tools_stage_not_executor_tail():
    from lawApp_LangGraph.audit_view import _stage_for_span

    executor = _stage(1, name="executor", offset=0)
    executor["ended_at"] = BASE + timedelta(seconds=2)
    tools = _stage(2, name="tools", offset=2)
    tools["ended_at"] = BASE + timedelta(seconds=4)
    # tool span 比 tools 节点 span 早 3ms 开启, 仍应归 tools
    span = {
        "run_id": "r1",
        "span_type": "tool",
        "started_at": BASE + timedelta(seconds=2, milliseconds=-3),
    }
    assert _stage_for_span(span, [executor, tools]) == 2


def test_context_inherits_missing_meta_and_flags_truncation():
    from lawApp_LangGraph.audit_view import build_context

    stages = [
        _stage(1, name="planner", offset=0),
        _stage(2, name="executor", offset=1),
    ]
    minors = {
        1: _minor(1, context_meta={
            "current_query": {"chars": 10, "hash": "a"},
            "truncated": True,
            "truncated_segments": ["current_query"],
        }),
        2: _minor(2, context_meta={}),
    }
    matrix = build_context(stages, minors)
    assert matrix.nodes[0].segments["current_query"].present is True
    assert matrix.nodes[1].inherited_from_stage_id == 1
    assert matrix.nodes[1].segments["current_query"].injected is False
    assert matrix.summary.truncated_segments == ["current_query"]


def test_verdicts_flag_unresolved_citation_as_warn():
    from lawApp_LangGraph.FastAPI.audit_model import (
        CitationEvidence,
        ContextMatrix,
        ContextNode,
        ContextSegmentInfo,
        ContextSummary,
        PerformanceBoard,
    )
    from lawApp_LangGraph.audit_view import derive_verdicts

    ev = EvidenceBoard(citations=[CitationEvidence(
        label="某案", type="case", evidence_ids=[], resolved=False)])
    ctx = ContextMatrix(
        segments=["current_query"],
        nodes=[ContextNode(
            stage_id=1, node_name="planner",
            segments={"current_query": ContextSegmentInfo(
                present=True, chars=10, hash="a", injected=True)},
        )],
        summary=ContextSummary(total_chars=10, history_turns=1),
    )
    perf = PerformanceBoard(total_ms=100, execution_ms=90)
    verdicts = derive_verdicts(
        {"run_id": "r1", "status": "ok", "mode": "attorney"},
        [], ev, ctx, perf, [],
    )
    assert verdicts.evidence.level == "warn"
    assert "未解析" in verdicts.evidence.summary
    assert verdicts.context.level == "good"


def test_evidence_verdict_warns_when_docs_have_no_usage_link():
    from lawApp_LangGraph.FastAPI.audit_model import (
        ContextMatrix,
        ContextNode,
        ContextSegmentInfo,
        ContextSummary,
        DocumentEvidence,
        PerformanceBoard,
    )
    from lawApp_LangGraph.audit_view import derive_verdicts

    ev = EvidenceBoard(documents=[DocumentEvidence(
        evidence_id="e1", kind="law", title="民法典第1087条",
        used_in_analysis=None, used_in_answer=None,
    )])
    ctx = ContextMatrix(
        segments=["current_query"],
        nodes=[ContextNode(
            stage_id=1, node_name="planner",
            segments={"current_query": ContextSegmentInfo(
                present=True, chars=10, hash="a", injected=True)},
        )],
        summary=ContextSummary(total_chars=10, history_turns=1),
    )
    verdicts = derive_verdicts(
        {"run_id": "r1", "status": "interrupted", "mode": "assistant"},
        [], ev, ctx, PerformanceBoard(total_ms=100),
        [],
    )
    assert verdicts.evidence.level == "warn"
    assert "未建立" in verdicts.evidence.summary


def test_evidence_id_is_stable_and_kind_scoped():
    from lawApp_LangGraph.evidence import evidence_id_for

    a = evidence_id_for("r1", "case", "（2023）京01民终1号")
    b = evidence_id_for("r1", "case", "（2023）京01民终1号")
    c = evidence_id_for("r1", "law", "（2023）京01民终1号")
    assert a == b and len(a) == 16
    assert a != c


def test_build_audit_view_chain_wait_and_partial_flag():
    from lawApp_LangGraph.audit_view import build_audit_view

    runs = [
        {"run_id": "p1", "session_id": "s1", "status": "interrupted",
         "mode": "attorney", "run_type": "live_ask",
         "started_at": BASE, "ended_at": BASE + timedelta(seconds=1),
         "final_answer": ""},
        {"run_id": "c1", "session_id": "s1", "status": "ok",
         "mode": "attorney", "run_type": "live_resume",
         "parent_run_id": "p1",
         "started_at": BASE + timedelta(seconds=5),
         "ended_at": BASE + timedelta(seconds=6),
         "final_answer": "终答"},
    ]
    stages = [
        _stage(1, rid="p1", name="ask_element", status="interrupted", offset=0),
        _stage(2, rid="c1", name="finalize", offset=5),
    ]
    view = build_audit_view("c1", True, runs, stages, {}, [])
    assert view.run.run_id == "c1"
    assert len(view.chain) == 2
    assert view.chain[1].wait_before_ms >= 4000
    assert view.verdicts.flow.level in ("good", "warn", "unknown")
    codes = {a.code for a in view.anomalies}
    assert "audit_partial" in codes
    assert view.degraded is True
