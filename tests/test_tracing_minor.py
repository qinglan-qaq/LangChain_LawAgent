"""tracing ↔ minor_states 钩子: 开行投影 / 闭行 phase / resume attempt。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lawApp_LangGraph import minor_state  # noqa: E402
from lawApp_LangGraph import tracing  # noqa: E402
from lawApp_LangGraph.state import AgentState, ElementQuestion  # noqa: E402


def _run(**kw):
    ctx = tracing.RunContext(
        run_id=kw.pop("run_id", "r1"),
        session_id="s1",
        run_type="live_ask",
        **kw,
    )
    tracing.set_run(ctx)
    return ctx


def test_open_minor_projects_input_and_attempt(monkeypatch):
    opened: list[dict] = []

    def _capture(run_id, session_id, node_name, seq, **kwargs):
        opened.append(
            {"run_id": run_id, "session_id": session_id,
             "node_name": node_name, "seq": seq, **kwargs}
        )

    monkeypatch.setattr(minor_state, "enqueue_open", _capture)
    run = _run(parent_run_id="parent-1")
    state = AgentState(query="离婚财产怎么分")
    tracing._open_minor(run, "ingest", 1, state)
    assert opened[0]["attempt"] == 2, "resume 首个节点 attempt=2"
    assert opened[0]["parent_run_id"] == "parent-1"
    assert "离婚财产怎么分" in opened[0]["input_text"]

    # 同一 resume run 的后续节点不得再次抬 attempt(否则一路 attempt=2)
    tracing._open_minor(run, "planner", 1, state)
    assert opened[1]["attempt"] == 1
    assert run.attempt_bumped is True


def test_close_minor_interrupt_marks_waiting_human(monkeypatch):
    closed: list[dict] = []

    def _capture(run_id, node_name, seq, **kwargs):
        closed.append({"node_name": node_name, "seq": seq, **kwargs})

    monkeypatch.setattr(minor_state, "enqueue_close", _capture)
    run = _run()
    state = AgentState(
        query="q",
        pending_questions=[ElementQuestion(key="marriage_status", question="结婚几年?")],
    )
    tracing._close_minor(run, "ask_element", 1, "interrupted", 12, None, state)
    assert closed[0]["phase"] == "waiting_human"
    assert "结婚几年" in closed[0]["output_json"]["question"]
    assert "等待用户回答" in closed[0]["output_text"]
    assert closed[0]["error"] is None


def test_close_minor_error_records_error(monkeypatch):
    closed: list[dict] = []
    monkeypatch.setattr(
        minor_state, "enqueue_close",
        lambda run_id, node_name, seq, **kw: closed.append(kw),
    )
    run = _run()
    tracing._close_minor(
        run, "planner", 1, "error", 5, ValueError("bad plan"), AgentState(query="q")
    )
    assert closed[0]["phase"] == "done"
    assert "ValueError" in closed[0]["error"]
    assert "bad plan" in closed[0]["output_json"]["error"]


def test_close_minor_ok_projects_output(monkeypatch):
    closed: list[dict] = []
    monkeypatch.setattr(
        minor_state, "enqueue_close",
        lambda run_id, node_name, seq, **kw: closed.append(kw),
    )
    run = _run()
    state = AgentState(query="q")
    tracing._close_minor(
        run, "chitchat", 1, "ok", 7, {"final_answer": "你好呀"}, state
    )
    assert closed[0]["phase"] == "done"
    assert closed[0]["output_text"] == "你好呀"
    assert closed[0]["refs"]["trace_span"]["node_name"] == "chitchat"


def test_open_minor_projection_failure_is_swallowed(monkeypatch):
    def _boom(*_a, **_kw):
        raise RuntimeError("projection broken")

    monkeypatch.setattr(minor_state, "enqueue_open", _boom)
    run = _run()
    tracing._open_minor(run, "ingest", 1, AgentState(query="q"))  # 不抛


def test_close_minor_projection_failure_is_swallowed(monkeypatch):
    def _boom(*_a, **_kw):
        raise RuntimeError("writer broken")

    monkeypatch.setattr(minor_state, "enqueue_close", _boom)
    run = _run()
    tracing._close_minor(run, "merge", 1, "ok", 1, {}, AgentState(query="q"))


def test_minor_context_meta_from_run():
    run = _run()
    run.context_meta["planner"] = {"current_query": {"chars": 3, "hash": "x"}}
    meta = tracing._minor_context_meta(run, "planner")
    assert meta["current_query"]["chars"] == 3
    assert tracing._minor_context_meta(run, "merge") == {}
