"""transcript: checkpoint messages → 轮次; 本地 JSON 导出."""

import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from lawApp_LangGraph import transcript


def test_build_turns_pairs_qa_and_skips_tool():
    msgs = [
        HumanMessage(content="问1"),
        AIMessage(content=""),
        ToolMessage(content='{"x": 1}', tool_call_id="t1"),
        AIMessage(content="答1"),
        HumanMessage(content="问2"),
        HumanMessage(content="[补充] 补2"),
        AIMessage(content="答2"),
    ]
    turns = transcript.build_turns(msgs)
    assert [t["question"] for t in turns] == ["问1", "问2"]
    assert [t["answer"] for t in turns] == ["答1", "答2"]
    assert turns[1]["supplements"] == ["补2"]
    assert turns[0]["turn"] == 1 and turns[1]["turn"] == 2


def test_build_turns_drops_empty_and_keeps_last_ai():
    turns = transcript.build_turns(
        [
            HumanMessage(content="问"),
            AIMessage(content="旧答"),
            AIMessage(content="新答"),
        ]
    )
    assert len(turns) == 1
    assert turns[0]["answer"] == "新答"


def test_attach_dialogue_matches_exact_answer_only():
    turns = [
        {"turn": 1, "question": "问1", "answer": "答1", "citations": [], "ts": None},
        {"turn": 2, "question": "问2", "answer": "闲聊", "citations": [], "ts": None},
    ]
    events = [
        {
            "event_type": "final_answer",
            "payload": {"answer": "答1", "citations": ["民法典 第1条"]},
            "created_at": None,
        }
    ]
    transcript.attach_dialogue(turns, events)
    assert turns[0]["citations"] == ["民法典 第1条"]
    assert turns[1]["citations"] == []


def test_attach_runs_by_query_and_answer():
    turns = [
        {"turn": 1, "question": "问1", "answer": "答1", "citations": [], "ts": None, "run_id": None},
        {"turn": 2, "question": "问2", "answer": "答2", "citations": [], "ts": None, "run_id": None},
    ]
    runs = [
        {"run_id": "r1", "query": "问1", "final_answer": ""},
        {"run_id": "r2", "query": "", "final_answer": "答2"},
    ]
    transcript.attach_runs(turns, runs)
    assert turns[0]["run_id"] == "r1"
    assert turns[1]["run_id"] == "r2"


def test_export_dir_for_sanitizes_traversal():
    path = transcript.export_dir_for("../../evil/AT-1")
    assert ".." not in path.parts
    assert path.name == ".._.._evil_AT-1"


def test_build_transcript_enriches_legacy_citation_labels():
    msgs = [HumanMessage(content="问1"), AIMessage(content="答1")]
    events = [
        {
            "event_type": "final_answer",
            "payload": {"answer": "答1", "citations": ["民法典 第1条"]},
            "created_at": None,
        }
    ]
    state = {
        "messages": msgs,
        "law_results": [
            {"law_title": "民法典", "article_number": "第1条", "content": "条文正文"}
        ],
        "rag_documents": [],
    }
    out = transcript.build_transcript("AT-1", state, events, [])
    assert out["turns"][0]["citations"] == [
        {"label": "民法典 第1条", "type": "law", "content": "条文正文"}
    ]


def test_write_export_atomic_json(monkeypatch, tmp_path):
    monkeypatch.setattr(transcript.settings, "export_dir", str(tmp_path))
    info = transcript.write_export(
        {"session_id": "AT-1", "turns": [{"turn": 1, "question": "问"}]}
    )
    path = tmp_path / "AT-1" / info["file"].split("/")[-1]
    assert path.exists()
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["schema_version"] == transcript.EXPORT_SCHEMA_VERSION
    assert body["session_id"] == "AT-1"
    assert body["turns"][0]["question"] == "问"
    assert info["bytes"] > 0 and len(info["sha256"]) == 64
