"""闲聊轻量人设(D10) + prompt JSON 上下文契约。"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

from langchain_core.runnables import RunnableLambda

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lawApp_LangGraph import prompts  # noqa: E402


def _text_of(value) -> str:
    """StringPromptValue 的 str() 是 repr 包装, 取真实文本。"""
    return getattr(value, "text", None) or str(value)


def _json_blocks(text: str) -> list[dict]:
    """抓 prompt 中 ## 上下文(JSON) 之后的 JSON 对象(容忍后文 schema 示例)。"""
    marker = "## 上下文(JSON"
    out = []
    start = 0
    decoder = json.JSONDecoder()
    while True:
        i = text.find(marker, start)
        if i < 0:
            break
        j = text.find("{", i)
        decoded = None
        if j >= 0:
            try:
                decoded, end = decoder.raw_decode(text, j)
            except Exception:
                decoded = None
        if decoded is not None:
            out.append(decoded)
            start = end
        else:
            start = i + len(marker)
    return out


def test_chitchat_persona_is_traits_only():
    block = prompts.CHITCHAT_PERSONA_BLOCK
    assert "冷静务实" in block and "AI 法律助手" in block
    assert "110" in block and "12338" in block
    # 不注入完整角色: 姓名/履历/领域话术不得出现
    for forbidden in ("Kim Wexler", "Profile", "Domain Expertise", "金式战略"):
        assert forbidden not in block


def test_chitchat_prompt_uses_persona_and_context_json():
    tpl = prompts.CHITCHAT_PROMPT
    assert "{persona}" in tpl
    assert "{context_json}" in tpl
    assert "{query}" not in tpl


def test_finalize_keeps_full_persona():
    assert "Kim Wexler" in prompts.FINALIZE_CASE_PROMPT.template
    assert "Kim Wexler" in prompts.FINALIZE_DIRECT_PROMPT.template
    assert "Kim Wexler" in prompts.LEGAL_ANALYSIS_PROMPT_KIM.template


def test_migrated_prompts_use_context_json():
    for tpl in (
        prompts.PLANNER_SYSTEM,
        prompts.EXECUTOR_PROMPT,
        prompts.ELEMENT_ASSESS_PROMPT,
        prompts.REPLAN_CHECK_PROMPT,
        prompts.CHITCHAT_PROMPT,
        prompts.FINALIZE_CASE_PROMPT.template,
        prompts.FINALIZE_DIRECT_PROMPT.template,
        prompts.FINALIZE_COMPLAINT_PROMPT,
        prompts.FINALIZE_DEFENSE_PROMPT,
    ):
        assert "{context_json}" in tpl
        assert "{query}" not in tpl
        assert "{elements_digest}" not in tpl


def test_chitchat_node_prompt_carries_persona_and_history(monkeypatch):
    import lawApp_LangGraph.LangGraph_lawApp as app
    from langchain_core.messages import AIMessage, HumanMessage

    captured: list[str] = []

    def _fake(prompt_text):
        captured.append(_text_of(prompt_text))
        return AIMessage(content="你好呀")

    monkeypatch.setattr(app, "get_executor_llm", lambda: RunnableLambda(_fake))
    state = app.AgentState(
        query="你还记得我问过什么吗",
        messages=[HumanMessage(content="我上次问离婚"), AIMessage(content="记得")],
    )
    asyncio.run(app.chitchat_node(state, {"configurable": {"thread_id": "T-P1"}}))
    text = captured[0]
    assert "说话方式" in text and "AI 法律助手" in text
    assert "Kim Wexler" not in text
    envelopes = _json_blocks(text)
    assert envelopes, "chitchat prompt 必须含 JSON 上下文"
    env = envelopes[0]
    assert list(env)[:1] == ["current_query"]
    assert env["current_query"] == "你还记得我问过什么吗"
    assert "history" in env and "critical_facts" not in env


def test_planner_prompt_envelope_priority_order(monkeypatch):
    import lawApp_LangGraph.LangGraph_lawApp as app

    captured: list[str] = []

    async def _fake_stream_plan(prompt_text, source, config):
        captured.append(_text_of(prompt_text))
        return app.PlanSchema.model_validate({"reasoning": [], "plan": []}), []

    monkeypatch.setattr(app, "_stream_plan", _fake_stream_plan)
    state = app.AgentState(
        query="房子怎么分",
        user_supplements=["婚后共同还贷"],
        conversation_memory={"facts": {"marriage": "在婚"}, "recent": []},
    )
    asyncio.run(app.planner_node(state, {"configurable": {"thread_id": "T-P2"}}))
    envelope = _json_blocks(captured[0])[0]
    keys = list(envelope)
    assert keys[0] == "current_query"
    assert keys[1] == "critical_facts"
    assert keys[-1] == "constraints"
    assert set(envelope["critical_facts"]["user_supplements"]) == {"婚后共同还贷"}
