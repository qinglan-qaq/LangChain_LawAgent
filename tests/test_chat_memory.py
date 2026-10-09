"""L13 记忆闭环 + L3 免责尾行 + L4 要素显隐标记 用例。

覆盖:
- _recent_history: 过滤空 AIMessage 占位 / 剔除当前轮 user 条 / 截断上限
- _query_with_supplements: 历史段前置拼入; 无历史时视图不变(向后兼容)
- _assistant_message_update: 追加 assistant 条 + 幂等防重
- planner prompt 组装带上历史(读侧闭环)
- ingest_node 不清 messages(reducer 跨轮持久的前提)
- 免责尾行: 3 个 prompt 模板不再指示 LLM 附「以上内容由 AI 生成」尾行
- add_messages 兼容 dict 输入(API 写侧传 {"role","content"})
"""
import types
import asyncio

from langchain_core.messages import AIMessage, HumanMessage


def _state(**kw):
    import lawApp_LangGraph.LangGraph_lawApp as app

    base = dict(query="当前问题", user_supplements=[])
    base.update(kw)
    return app.AgentState(**base)


# ── L13: 读侧 _recent_history ──


def test_recent_history_filters_empty_and_current_turn():
    from lawApp_LangGraph.LangGraph_lawApp import _recent_history

    state = _state(
        messages=[
            HumanMessage(content="第一轮问题"),
            AIMessage(content=""),
            AIMessage(content="第一轮回答"),
            HumanMessage(content="当前问题"),  # 当前轮(API 写侧刚写入)
            AIMessage(content=""),  # 图内空占位
        ]
    )
    hist = _recent_history(state)
    assert "[最近对话历史]" in hist
    assert "第一轮问题" in hist and "第一轮回答" in hist
    assert "当前问题" not in hist, "当前轮 user 条不得重复进历史"
    assert "用户: 第一轮问题" in hist and "助手: 第一轮回答" in hist


def test_recent_history_no_messages_returns_empty():
    """无 messages(SimpleNamespace 兼容)或无有效内容 → 空串。"""
    from lawApp_LangGraph.LangGraph_lawApp import _recent_history

    assert _recent_history(_state(messages=[])) == ""
    assert _recent_history(types.SimpleNamespace(query="q")) == ""


def test_recent_history_limits():
    from lawApp_LangGraph.LangGraph_lawApp import (
        _HISTORY_MSG_LIMIT,
        _HISTORY_PER_MSG_LIMIT,
        _HISTORY_TOTAL_LIMIT,
        _recent_history,
    )

    # 窗口: 10 轮短消息 → 只保留最近 _HISTORY_MSG_LIMIT 条
    msgs = []
    for i in range(10):
        msgs.append(HumanMessage(content=f"问{i}"))
        msgs.append(AIMessage(content=f"答{i}"))
    hist = _recent_history(_state(messages=msgs))
    assert "问0" not in hist and "答0" not in hist, "只保留最近几条"
    assert f"问{10 - _HISTORY_MSG_LIMIT // 2}" in hist, "窗口最旧一条在"

    # 单条截断: 400 字内容 → 截到 _HISTORY_PER_MSG_LIMIT
    hist2 = _recent_history(
        _state(
            messages=[
                HumanMessage(content="问 " + "x" * 400),
                AIMessage(content="答 " + "y" * 400),
            ]
        )
    )
    assert "x" * _HISTORY_PER_MSG_LIMIT not in hist2, "单条截断"
    assert "x" * (_HISTORY_PER_MSG_LIMIT - 3) in hist2

    # 总量上限(含行前缀): 多轮长消息 → 整段封顶
    msgs3 = []
    for i in range(10):
        msgs3.append(HumanMessage(content=f"问{i} " + "x" * 400))
        msgs3.append(AIMessage(content=f"答{i} " + "y" * 400))
    hist3 = _recent_history(_state(messages=msgs3))
    assert len(hist3) <= len("[最近对话历史]\n") + _HISTORY_TOTAL_LIMIT + 8


def test_query_view_prepends_history_and_keeps_compat():
    """历史段在视图最前; 无历史时行为与旧版一致(原 query 打头)。"""
    from lawApp_LangGraph.LangGraph_lawApp import _query_with_supplements

    state = _state(
        query="当前问题",
        messages=[HumanMessage(content="旧问"), AIMessage(content="旧答")],
        user_supplements=["补充内容"],
    )
    view = _query_with_supplements(state)
    assert view.index("[最近对话历史]") == 0, "历史段前置"
    assert "当前问题" in view and "补充内容" in view
    assert view.index("旧问") < view.index("当前问题")

    # 无历史 → 视图以原 query 打头(既有用例 test_m8 的契约)
    view2 = _query_with_supplements(
        types.SimpleNamespace(query="Q", user_supplements=[])
    )
    assert view2 == "Q"


# ── L13: 写侧 _assistant_message_update ──


def test_assistant_message_update_append_and_idempotent():
    from lawApp_LangGraph.LangGraph_lawApp import _assistant_message_update

    state = _state(messages=[HumanMessage(content="问")])
    out = _assistant_message_update(state, "答")
    assert isinstance(out["messages"][0], AIMessage)
    assert out["messages"][0].content == "答"

    # 尾部已是同文 assistant 条(重复进 finalize) → 不追加
    state2 = _state(
        messages=[HumanMessage(content="问"), AIMessage(content="答")]
    )
    assert _assistant_message_update(state2, "答") == {}
    # 尾部是别的答案 → 追加
    assert _assistant_message_update(state2, "新答") != {}


def test_planner_prompt_includes_history(monkeypatch):
    """planner 组 prompt 时带上历史(读侧闭环生效)。"""
    import lawApp_LangGraph.LangGraph_lawApp as app

    captured = []

    async def _fake_stream_plan(prompt_text, source, config):
        captured.append(prompt_text)
        plan = app.PlanSchema.model_validate({"reasoning": [], "plan": []})
        return plan, []

    monkeypatch.setattr(app, "_stream_plan", _fake_stream_plan)
    state = app.AgentState(
        query="第二问",
        messages=[HumanMessage(content="第一问"), AIMessage(content="第一答")],
    )
    asyncio.run(app.planner_node(state, {"configurable": {"thread_id": "T-L13"}}))
    assert captured
    assert "第一问" in captured[0] and "第一答" in captured[0], "历史必须进 planner prompt"


def test_ingest_does_not_wipe_messages():
    """ingest 每轮清回合字段, 但不得返回 messages 清空(reducer 持久前提)。"""
    import lawApp_LangGraph.LangGraph_lawApp as app

    state = app.AgentState(
        query="q",
        messages=[HumanMessage(content="旧问"), AIMessage(content="旧答")],
    )
    out = app.ingest_node(state)
    assert "messages" not in (out or {}), "ingest 不得动 messages"


def test_chitchat_uses_history_and_writes_message(monkeypatch):
    """chitchat 直接 END 不经 finalize: 须自带历史读侧 + assistant 写侧。"""
    import lawApp_LangGraph.LangGraph_lawApp as app
    from langchain_core.runnables import RunnableLambda

    captured = []

    def _fake(prompt_text):
        captured.append(str(prompt_text))
        return AIMessage(content="闲聊回应")

    monkeypatch.setattr(app, "get_executor_llm", lambda: RunnableLambda(_fake))
    state = app.AgentState(
        query="我刚才问了什么",
        messages=[HumanMessage(content="你好"), AIMessage(content="你好呀")],
    )
    out = asyncio.run(
        app.chitchat_node(state, {"configurable": {"thread_id": "T-L13C"}})
    )
    assert out["final_answer"] == "闲聊回应"
    assert out["messages"][0].content == "闲聊回应", "闲聊回应须入对话历史"
    assert "你好" in captured[0] and "你好呀" in captured[0], "chitchat prompt 须带历史"
    assert "我刚才问了什么" in captured[0], "当前问题仍在视图主体"


def test_add_messages_accepts_dict_input():
    """API 写侧传 {"role","content"} dict, add_messages 需归一为消息对象。"""
    from langgraph.graph.message import add_messages

    out = add_messages([], [{"role": "user", "content": "你好"}])
    assert isinstance(out[0], HumanMessage) and out[0].content == "你好"


# ── L3: 免责尾行改前端固定小字, prompt 不再指示 ──


def test_prompts_no_disclaimer_tail_instruction():
    from lawApp_LangGraph import prompts

    for tpl in (
        prompts.FINALIZE_CASE_PROMPT,
        prompts.FINALIZE_DIRECT_PROMPT,
        prompts.LEGAL_ANALYSIS_PROMPT_KIM,
    ):
        assert "末尾附一行" not in tpl.template, f"{tpl} 仍指示免责尾行"
        assert "不构成正式法律意见」" not in tpl.template


# ── L2: 工具调用 SSE 定位(executor doing / merge 完成步骤) ──


def test_sse_tool_call_and_result_helpers():
    """executor 成功路径不回写 current_step_index → 以 doing 定位;
    merge 回写 idx+1 → 以 plan[ci-1] 定位刚完成步骤(覆盖全部工具)。"""
    from lawApp_LangGraph.FastAPI.api import (
        _executor_tool_names,
        _merge_tool_summary,
    )
    from lawApp_LangGraph.state import PlanStep

    # executor: doing 步骤 → 工具名
    plan = [
        PlanStep(step_id=1, description="d1", tool_name="search_cases", status="doing"),
        PlanStep(step_id=2, description="d2", tool_name="fetch_laws", status="pending"),
    ]
    assert _executor_tool_names({"plan": plan}) == ["search_cases"]
    assert _executor_tool_names({"plan": [], "current_step_index": 1}) == []
    # 无工具步骤(plan 无 doing)不发
    assert (
        _executor_tool_names(
            {"plan": [PlanStep(step_id=1, description="d", tool_name=None, status="done")]}
        )
        == []
    )

    # merge: 数据检索键计数拼进摘要
    done = PlanStep(step_id=1, description="d", tool_name="fetch_laws", status="done")
    upd = {"plan": [done], "current_step_index": 1, "law_results": [1, 2, 3]}
    assert _merge_tool_summary(upd) == "law_results 3"
    # 非检索工具 → 执行完成/执行失败
    upd2 = {
        "plan": [
            PlanStep(step_id=1, description="d", tool_name="save_to_memory", status="done")
        ],
        "current_step_index": 1,
    }
    assert _merge_tool_summary(upd2) == "执行完成"
    failed = PlanStep(step_id=1, description="d", tool_name="web", status="failed")
    assert _merge_tool_summary({"plan": [failed], "current_step_index": 1}) == "执行失败"
    # 无工具名 / 越界 → None
    assert (
        _merge_tool_summary(
            {
                "plan": [PlanStep(step_id=1, description="d", tool_name=None)],
                "current_step_index": 1,
            }
        )
        is None
    )
    assert _merge_tool_summary({"plan": [], "current_step_index": 3}) is None


# ── L4: 要素显隐标记 ──


def test_elements_sse_payload_has_case_flag():
    """api.py 两处 elements 事件 payload 带 is_case_query(chitchat → false)。

    chitchat 语义本身由 test_trace_e2e 覆盖; 此处锁定 SSE 发射点的
    payload 契约(前端 ElementPanel 依赖该标记决定显隐)。
    """
    import inspect

    from lawApp_LangGraph.FastAPI import api

    src = inspect.getsource(api)
    assert src.count("is_case_query") >= 2, "两处发射点(sync generator + out_q)均需带标记"
    assert '!= "chitchat"' in src, "标记须由 question_category 派生"


# ── 监控双视图 A1: 摘要逐条字符截断全去除(全文进 prompt, 条数上限保留) ──


def test_step_summaries_pass_full_text():
    """_step_summaries 摘要不再逐条截断: 法条/检索 chunk/web 摘要
    全文进入; 条数上限(law×5 / rag×3 / web×3)保留。"""
    from lawApp_LangGraph.LangGraph_lawApp import _step_summaries
    from lawApp_LangGraph.state import LawsResult, RetrievedDocument

    long_law = "法" * 300
    long_chunk = "案" * 300
    long_snippet = "网" * 300
    state = _state(
        law_results=[
            LawsResult(law_title=f"法律{i}", article_number=str(i), content=long_law)
            for i in range(8)
        ],
        rag_documents=[
            RetrievedDocument(case_number=f"case-{i}", chunk_text=long_chunk)
            for i in range(6)
        ],
        web_search_results=[
            {"title": f"t{i}", "snippet": long_snippet} for i in range(5)
        ],
    )
    s = _step_summaries(state)
    # 全文进入, 无截断省略号
    assert long_law in s["law_summary"] and "..." not in s["law_summary"]
    assert long_chunk in s["rag_summary"]
    assert long_snippet in s["web_summary"]
    # 条数上限保留
    assert s["law_summary"].count("[法律") == 5
    assert s["rag_summary"].count("[0.") + s["rag_summary"].count("]") >= 3
    assert s["web_summary"].count("[t") == 3


def test_prompt_assembly_has_no_per_item_truncation():
    """A1 回归: prompt 组装层逐条字符截断全删 — 源码扫描
    (检索类字段 content/chunk_text/snippet 的切片与 fetch_laws 的
    源头 [:600] 均不得残留); query 整问护栏/日志截断不在本列。"""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    app_src = (root / "lawApp_LangGraph" / "LangGraph_lawApp.py").read_text(
        encoding="utf-8"
    )
    tools_src = (root / "lawApp_LangGraph" / "tools" / "db_tools.py").read_text(
        encoding="utf-8"
    )
    for frag in (
        "chunk_text[:", "law.content[:", "l.content[:", "snippet[:",
    ):
        assert frag not in app_src, f"摘要逐条截断残留: {frag}"
    assert '(r[3] or "")[:' not in tools_src, "fetch_laws 源头 600 字截断残留"
