"""minor_state 投影纯函数测试 — 16 节点 / 清洗 / 截断 / 异常旁路。"""

from __future__ import annotations

import json

from lawApp_LangGraph.minor_state import project_input, project_output

NODES = [
    "ingest",
    "risk_gate",
    "element_assess",
    "ask_element",
    "chitchat",
    "planner",
    "executor",
    "tools",
    "merge",
    "replan_check",
    "mid_clarify",
    "hitl_degrade",
    "hitl_budget",
    "replanner",
    "element_fill",
    "finalize",
]


def _state(**kw):
    base = {
        "query": "我想离婚并争取女儿抚养权",
        "mode": "attorney",
        "doc_type": "",
        "case_elements": {
            "elements": [
                {"key": "marriage_status", "label": "婚姻现状", "status": "known", "value": "已婚8年"},
                {"key": "property", "label": "财产", "status": "missing", "value": ""},
            ]
        },
        "pending_questions": [
            {"key": "property", "question": "房产是婚前还是婚后购买?", "options": ["婚前", "婚后"]}
        ],
        "clarify_history": [
            {"round": 1, "question": "婚姻状态?", "answer": "已婚"}
        ],
        "clarify_rounds": 1,
        "plan": [
            {"step_id": 1, "description": "检索案例", "tool_name": "retrieve_legal_knowledge", "args": {"top_k": 5}}
        ],
        "current_step_index": 0,
        "rag_documents": [
            {
                "case_number": "(2023)京01民终123号",
                "case_cause": "离婚纠纷",
                "chunk_text": "婚后房产原则上均分" * 50,
                "relevance_score": 0.81,
                "rrf_score": 0.03,
            }
        ],
        "law_results": [{"law_title": "民法典", "article_number": "第1087条", "content": "离婚时……"}],
        "web_search_results": [{"title": "解读", "link": "https://x", "snippet": "摘要"}],
        "evaluation": {"quality_verdict": "insufficient"},
        "messages": [],
        "tool_calls": [],
        "conversation_memory": {},
        "element_fill_rounds": 0,
        "insufficient_reason": "not_found",
        "error": None,
        "error_streak": 0,
    }
    base.update(kw)
    return base


def test_all_16_nodes_project_without_raising():
    st = _state()
    rets = {
        "planner": {"plan": st["plan"]},
        "executor": {"plan": st["plan"], "messages": []},
        "tools": {"messages": []},
        "merge": {"current_step_index": 1},
        "replan_check": {"replan_needed": True, "insufficient_reason": "not_found"},
        "element_assess": {"case_elements": st["case_elements"], "pending_questions": st["pending_questions"]},
        "finalize": {"final_answer": "结论"},
        "chitchat": {"final_answer": "你好"},
    }
    for node in NODES:
        text, meta = project_input(node, st)
        assert isinstance(text, str), node
        assert isinstance(meta, dict), node
        out_text, out_json = project_output(node, rets.get(node, {}), st)
        assert isinstance(out_text, str), node
        assert isinstance(out_json, dict), node


def test_blacklist_and_sanitize():
    st = _state(
        messages=[{"role": "user", "content": "secret", "type": "human"}],
        prompts_record={"query": "secret"},
        final_prompts="TOP SECRET PROMPT",
        rag_documents=[
            {"case_number": "A", "chunk_text": "正文" * 500, "relevance_score": 0.9}
        ],
    )
    text, _ = project_input("planner", st)
    assert "TOP SECRET PROMPT" not in text
    out_text, out_json = project_output(
        "finalize",
        {
            "final_answer": "<html><script>alert(1)</script>",
            "final_prompts": "TOP SECRET",
            "messages": ["x"],
        },
        st,
    )
    assert "TOP SECRET" not in json.dumps(out_json, ensure_ascii=False)
    assert "<script" not in json.dumps(out_json, ensure_ascii=False)
    assert "<script" not in out_text


def test_truncation_and_json_shrink(monkeypatch):
    from lawApp_LangGraph.config import settings

    monkeypatch.setattr(settings, "minor_input_text_limit", 100)
    monkeypatch.setattr(settings, "minor_output_text_limit", 100)
    monkeypatch.setattr(settings, "minor_json_max_bytes", 200)
    st = _state(query="长" * 500)
    text, meta = project_input("risk_gate", st)
    assert "[截断" in text
    assert meta.get("truncated_chars", {}).get("input", 0) > 0

    from lawApp_LangGraph.minor_state import _shrink_json

    _, out = project_output("merge", {"current_step_index": 1}, st)
    out, shrunk = _shrink_json({**out, "blob": "x" * 2000}, 200)
    assert shrunk is True
    assert len(json.dumps(out, ensure_ascii=False).encode()) <= 400


def test_projection_exception_does_not_raise(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("lawApp_LangGraph.minor_state._input_parts", boom)
    monkeypatch.setattr("lawApp_LangGraph.minor_state._output_parts", boom)
    text, meta = project_input("executor", _state())
    assert text == "输入投影失败"
    assert "projection_error" in meta
    out_text, out_json = project_output("executor", {}, _state())
    assert out_text == ""
    assert "projection_error" in out_json


def test_rag_preview_limited():
    st = _state(rag_documents=[
        {"case_number": f"C{i}", "chunk_text": "x" * 500} for i in range(10)
    ])
    _, out = project_output("tools", {"messages": []}, st)
    # tools 节点从 ToolMessage 解析, 无消息时计数为 0; planner 投影走 state
    text, _ = project_input("executor", st)
    assert "案例: 10 条" in text
