"""element_fill(D9) — 伪工具 → 路由 → HITL 补全 → 步骤推进。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _state(**kw):
    import lawApp_LangGraph.LangGraph_lawApp as app

    base = dict(query="我想离婚", mode="attorney")
    base.update(kw)
    return app.AgentState(**base)


def _missing_key(state):
    return next(e.key for e in state.case_elements.elements if e.status == "missing")


# ── 伪工具规划与执行 ──


def test_normalize_plan_keeps_element_fill_and_args():
    import lawApp_LangGraph.LangGraph_lawApp as app

    plan = app.PlanSchema.model_validate(
        {
            "reasoning": [],
            "plan": [
                {
                    "step_id": 1,
                    "description": "补充婚姻现状",
                    "tool_name": "request_element_fill",
                    "args": {"keys": ["marriage_status"]},
                }
            ],
        }
    )
    steps = app._normalize_plan(plan)
    assert steps[0].tool_name == "request_element_fill"
    assert steps[0].args == {"keys": ["marriage_status"]}
    assert "request_element_fill" in app._tools_desc()


def test_executor_element_fill_step_sets_pending_without_advancing():
    import lawApp_LangGraph.LangGraph_lawApp as app
    from lawApp_LangGraph.state import PlanStep

    state = _state(
        plan=[
            PlanStep(
                step_id=1,
                description="补充要素",
                tool_name="request_element_fill",
                args={"keys": ["marriage_status"]},
            )
        ],
        current_step_index=0,
    )
    out = asyncio.run(
        app.executor_node(state, {"configurable": {"thread_id": "T-EF1"}})
    )
    assert out["pending_fill_keys"] == ["marriage_status"]
    assert "current_step_index" not in out, "伪工具步不推进, 由 element_fill 收口"
    assert "messages" not in out, "伪工具不产生 tool_call"


def test_executor_element_fill_falls_back_to_critical_missing():
    import lawApp_LangGraph.LangGraph_lawApp as app
    from lawApp_LangGraph.state import PlanStep

    state = _state(
        plan=[
            PlanStep(
                step_id=1,
                description="补充要素",
                tool_name="request_element_fill",
                args={},
            )
        ],
    )
    out = asyncio.run(
        app.executor_node(state, {"configurable": {"thread_id": "T-EF2"}})
    )
    assert out["pending_fill_keys"], "无显式 keys 时取关键缺口"
    assert set(out["pending_fill_keys"]) <= {
        e.key for e in state.case_elements.elements
    }


def test_route_after_executor_to_element_fill():
    import lawApp_LangGraph.LangGraph_lawApp as app

    state = _state(pending_fill_keys=["marriage_status"])
    assert app.route_after_executor(state) == "element_fill"
    # 非空 pending 优先于普通步骤推进
    state2 = _state(
        pending_fill_keys=["marriage_status"],
        plan=[],
        current_step_index=0,
    )
    assert app.route_after_executor(state2) == "element_fill"


def test_graph_has_element_fill_node_and_edge():
    import lawApp_LangGraph.LangGraph_lawApp as app

    g = app.build_graph()
    graph = g.get_graph()
    assert "element_fill" in graph.nodes
    edges = {(e.source, e.target) for e in graph.edges}
    assert ("element_fill", "executor") in edges


# ── 节点行为 ──


def test_element_fill_no_pending_advances():
    import lawApp_LangGraph.LangGraph_lawApp as app

    state = _state(plan=[], current_step_index=2)
    out = app.element_fill_node(state, None)
    assert out == {"pending_fill_keys": [], "current_step_index": 3}


def test_element_fill_cap_reached_no_interrupt(monkeypatch):
    import lawApp_LangGraph.LangGraph_lawApp as app

    def _boom(_payload):
        raise AssertionError("达上限不得 interrupt")

    monkeypatch.setattr(app, "interrupt", _boom)
    key = _missing_key(_state())
    state = _state(
        pending_fill_keys=[key],
        element_fill_rounds=app.settings.max_element_fill_rounds,
        current_step_index=1,
    )
    out = app.element_fill_node(state, None)
    assert out["pending_fill_keys"] == []
    assert out["current_step_index"] == 2
    assert out["hitl_event"]["reason"] == "fill_cap_reached"


def test_element_fill_answer_writes_supplement_history_and_message(monkeypatch):
    import lawApp_LangGraph.LangGraph_lawApp as app

    captured = {}

    def _fake_interrupt(payload):
        captured.update(payload)
        return "结婚 5 年, 有一个 3 岁孩子"

    monkeypatch.setattr(app, "interrupt", _fake_interrupt)
    state0 = _state()
    key = _missing_key(state0)
    label = next(e.label for e in state0.case_elements.elements if e.key == key)
    state = _state(
        pending_fill_keys=[key],
        plan=[],
        current_step_index=0,
        clarify_rounds=2,
    )
    out = app.element_fill_node(state, None)

    assert captured["type"] == "element_fill"
    assert captured["key"] == key
    assert label in captured["question"]
    assert out["pending_fill_keys"] == []
    assert out["current_step_index"] == 1
    assert out["element_fill_rounds"] == 1
    assert out["user_supplements"] == ["结婚 5 年, 有一个 3 岁孩子"]
    assert out["clarify_history"][0].round == 3
    assert out["clarify_history"][0].element_keys == [key]
    elem = next(e for e in out["case_elements"].elements if e.key == key)
    assert elem.status == "known" and elem.updated_by == "element_fill"
    assert out["messages"][0].content == "[补充] 结婚 5 年, 有一个 3 岁孩子"
    assert out["hitl_event"]["type"] == "element_fill"


def test_element_fill_skip_advances_without_supplement(monkeypatch):
    import lawApp_LangGraph.LangGraph_lawApp as app

    monkeypatch.setattr(app, "interrupt", lambda _p: "")
    key = _missing_key(_state())
    out = app.element_fill_node(
        _state(pending_fill_keys=[key], current_step_index=0), None
    )
    assert out["current_step_index"] == 1
    assert out["element_fill_rounds"] == 1
    assert out["hitl_event"]["skipped"] is True
    assert "user_supplements" not in out
    assert "messages" not in out


def test_element_fill_only_fills_missing_key(monkeypatch):
    """已 known 的 key 不在候选 → 不中断, 直接推进。"""
    import lawApp_LangGraph.LangGraph_lawApp as app

    state0 = _state()
    ce = state0.case_elements.model_copy(deep=True)
    key = ce.elements[0].key
    ce.update(key, "已填写", by="assess")
    monkeypatch.setattr(
        app, "interrupt", lambda _p: pytest.fail("已 known 不应 interrupt")
    )
    out = app.element_fill_node(
        _state(
            pending_fill_keys=[key],
            case_elements=ce,
            current_step_index=0,
        ),
        None,
    )
    assert out == {"pending_fill_keys": [], "current_step_index": 1}
