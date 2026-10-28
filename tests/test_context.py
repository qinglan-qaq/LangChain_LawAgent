"""context.build_context — 键顺序 / 空段省略 / 预算 / 不可压缩来源。"""

from __future__ import annotations

import json

from lawApp_LangGraph.context import build_context, render_context


def _state(**kw):
    base = {
        "query": "我想离婚",
        "mode": "attorney",
        "case_elements": {
            "elements": [
                {"key": "marriage_status", "status": "known", "value": "已婚"},
                {"key": "property", "status": "missing", "value": ""},
            ]
        },
        "user_supplements": ["对方转移财产"],
        "clarify_history": [
            {"round": 1, "question": "婚姻状态?", "answer": "已婚"}
        ],
        "conversation_memory": {
            "summary": {"conversation_goal": "离婚", "progress": ["已确认已婚"]},
            "facts": {"children": "1女6岁"},
            "recent": [{"role": "user", "text": "你好", "ts": "t"}],
            "retrieval_digest": [
                {"topic": "财产", "conclusion": "均分", "case_numbers": ["A"]}
            ],
        },
        "rag_documents": [
            {"case_number": "A", "case_cause": "离婚", "chunk_text": "原文" * 100,
             "relevance_score": 0.8, "rrf_score": 0.03}
        ],
        "law_results": [{"law_title": "民法典", "article_number": "1087", "content": "条文"}],
        "web_search_results": [{"title": "t", "link": "u", "snippet": "s"}],
        "plan": [{"step_id": 1, "description": "检索", "tool_name": "retrieve_legal_knowledge"}],
        "current_step_index": 0,
        "tool_calls": [{"step_id": 1}],
        "insufficient_reason": "not_found",
    }
    base.update(kw)
    return base


def test_key_order_and_empty_omission():
    env, _ = build_context(_state(), "executor")
    assert list(env.keys()) == [
        "current_query", "critical_facts", "history", "evidence", "task", "constraints"
    ]
    assert env["task"]["tool_name"] == "retrieve_legal_knowledge"

    env2, _ = build_context(_state(rag_documents=[], law_results=[], web_search_results=[]), "chitchat")
    assert "evidence" not in env2
    assert "critical_facts" not in env2
    assert list(env2.keys()) == ["current_query", "history", "task", "constraints"]


def test_uncompressible_fields_only_from_state():
    st = _state(
        conversation_memory={
            "summary": {"conversation_goal": "假目标"},
            "facts": {"marriage_status": "假事实"},
        },
        case_elements={
            "elements": [
                {"key": "marriage_status", "status": "known", "value": "真事实"}
            ]
        },
    )
    env, _ = build_context(st, "planner")
    assert env["critical_facts"]["case_elements"]["marriage_status"] == "真事实"
    assert env["history"]["facts"]["marriage_status"] == "假事实"
    assert env["current_query"] == "我想离婚"


def test_budget_truncation_and_meta(monkeypatch):
    from lawApp_LangGraph.config import settings

    monkeypatch.setattr(settings, "memory_per_msg_limit", 400)
    env, meta = build_context(_state(query="长" * 5000), "element_assess")
    assert len(env["current_query"]) <= 1000
    assert meta["current_query"]["chars"] <= 1000 + 50
    assert isinstance(meta["current_query"]["hash"], str)


def test_context_meta_written_to_run_context(monkeypatch):
    from lawApp_LangGraph.tracing import RunContext, set_run, _current_run

    run = RunContext(run_id="r", session_id="s", run_type="live_ask")
    set_run(run)
    build_context(_state(), "planner")
    assert "planner" in run.context_meta
    _current_run.set(None)


def test_render_is_valid_json():
    env, _ = build_context(_state(), "executor")
    text = render_context(env)
    assert json.loads(text) == env
    assert text.index("current_query") < text.index("constraints")
