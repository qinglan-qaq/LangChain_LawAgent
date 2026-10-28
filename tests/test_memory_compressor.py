"""记忆压缩 / 最近轮次渲染 — 字段级合并、失败保留、幂等。"""

from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from lawApp_LangGraph import memory


def _state(**kw):
    base = {
        "query": "本轮问题",
        "messages": [
            HumanMessage(content="第一轮"),
            AIMessage(content="第一轮回答"),
            HumanMessage(content="第二轮"),
            AIMessage(content="第二轮回答"),
        ],
        "clarify_history": [],
        "conversation_memory": {},
        "case_elements": {},
    }
    base.update(kw)
    return base


def test_render_recent_skips_tool_and_dedupes_current_query():
    st = _state(
        messages=[
            HumanMessage(content="q1"),
            ToolMessage(content="工具输出", tool_call_id="1"),
            AIMessage(content="a1"),
            HumanMessage(content="本轮问题"),
        ]
    )
    text = memory.render_recent(st)
    assert "工具输出" not in text
    assert "本轮问题" not in text
    assert "q1" in text and "a1" in text


def test_render_recent_uses_memory_recent():
    st = _state(
        conversation_memory={"recent": [{"role": "user", "text": "内存里的"}]},
        messages=[HumanMessage(content="消息里的")],
    )
    assert "内存里的" in memory.render_recent(st)
    assert "消息里的" not in memory.render_recent(st)


def test_maybe_compress_success_field_level(monkeypatch):
    async def fake(state):
        return {
            "summary_update": {
                "conversation_goal": "离婚",
                "progress": ["已确认已婚"],
                "open_items": ["证据不足"],
                "decisions": [],
            },
            "facts_patch": {"children": "1女6岁", "marriage_status": "已婚"},
            "retrieval_digest": [{"topic": "财产", "conclusion": "均分", "case_numbers": ["A"]}],
            "dropped": ["寒暄"],
        }

    monkeypatch.setattr(memory, "_compress_once", fake)
    st = _state(
        messages=[
            HumanMessage(content="q1"), AIMessage(content="a1"),
            HumanMessage(content="q2"), AIMessage(content="a2"),
            HumanMessage(content="q3"), AIMessage(content="a3"),
        ],
        conversation_memory={"summary": {"conversation_goal": "旧目标", "progress": ["旧"]}},
    )
    mem = asyncio.run(memory.maybe_compress(st))
    assert mem["summary"]["conversation_goal"] == "离婚"
    assert mem["summary"]["progress"] == ["已确认已婚"]
    assert mem["facts"]["children"] == "1女6岁"
    assert mem["compressed_until_turn"] == 3
    assert mem["version"] == 2
    assert mem["recent"]


def test_compress_failure_keeps_old(monkeypatch):
    async def boom(state):
        raise TimeoutError("slow")

    monkeypatch.setattr(memory, "_compress_once", boom)
    old = {"summary": {"conversation_goal": "旧"}, "recent": [{"role": "user", "text": "原文"}]}
    st = _state(
        messages=[
            HumanMessage(content=f"q{i}") for i in range(4)
        ],
        conversation_memory=old,
    )
    mem = asyncio.run(memory.maybe_compress(st))
    assert mem == old


def test_compress_idempotent_watermark(monkeypatch):
    calls = {"n": 0}

    async def fake(state):
        calls["n"] += 1
        return {"summary_update": {"conversation_goal": "g"}}

    monkeypatch.setattr(memory, "_compress_once", fake)
    st = _state(
        messages=[HumanMessage(content=f"q{i}") for i in range(3)],
        conversation_memory={"compressed_until_turn": 3},
    )
    asyncio.run(memory.maybe_compress(st))
    assert calls["n"] == 0


def test_digest_previous_retrieval():
    st = _state(
        rag_documents=[
            {"case_number": "A", "case_cause": "离婚", "chunk_text": "结论一句。后续"}
        ]
    )
    mem = asyncio.run(memory.digest_previous_retrieval(st))
    assert mem["retrieval_digest"][0]["case_numbers"] == ["A"]
    assert "结论一句" in mem["retrieval_digest"][0]["conclusion"]


def test_hitl_message_update_dedupes():
    st = _state(messages=[AIMessage(content="问"), HumanMessage(content="[补充] 已婚")])
    assert memory.hitl_message_update(st, "已婚") == {}
    out = memory.hitl_message_update(st, "房产婚后")
    assert out["messages"][0].content == "[补充] 房产婚后"
