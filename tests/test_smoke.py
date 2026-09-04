"""
冒烟测试 — upgrade-v1 重构验收 (pytest)

覆盖:
    1. 全仓 import(依赖 1.x 栈可加载)
    2. State reducers(append / RESET 清空)
    3. 图构建(节点拓扑 + InMemory checkpointer/store 装配)
    4. 空计划 → finalize 直接回答(monkeypatch LLM)
    5. HITL: 真实 clarify 节点 interrupt → Command(resume) 恢复
    6. 重启续聊: 同 thread_id 跨图实例状态保持 + 请求间累积字段重置

运行: /Users/qinglan/miniconda3/envs/lawagent/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest
from langchain_core.runnables import Runnable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


#  测试替身 — 必须是 Runnable:1.x 的 '|' 管道经 coerce_to_runnable 校验,
#  普通 mock 对象无法进入 RunnableSequence


class _FakeMsg:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class _FakeVerdict:
    """通用 structured output 替身,同时满足 Clarify / Plan / ReplanCheck
    三种 Schema 的属性形状(图内各节点按需读取各自属性)。"""

    def __init__(self, *, plan=(), reasoning=(), need_clarification=False,
                 question="", high_risk=False, needs_replan=False, reason=""):
        self.plan = list(plan)
        self.reasoning = list(reasoning)
        self.need_clarification = need_clarification
        self.question = question
        self.high_risk = high_risk
        self.needs_replan = needs_replan
        self.reason = reason


class _FakeChain(Runnable):
    """`PromptTemplate | llm.with_structured_output(...)` 的替身。
    invoke 为 Runnable 的抽象方法,必须实现(与 ainvoke 同结果)。"""

    def __init__(self, result=None):
        self.result = result

    def invoke(self, _inp, config=None, **kwargs):
        return self.result

    async def ainvoke(self, _inp, config=None, **kwargs):
        return self.result

    async def astream(self, _inp, config=None, **kwargs):
        yield _FakeMsg("测试回答")


class _FakeLLM(Runnable):
    """LLM 替身: with_structured_output 返回可控 verdict;bind_tools 返回自身
    (ainvoke 结果由 executor_result 控制,None 会让 executor 走步骤失败路径)。"""

    def __init__(self, state):
        self.state = state

    def invoke(self, msgs, config=None, **kwargs):
        return self.state["executor_result"]

    def with_structured_output(self, schema):
        return _FakeChain(result=self.state["plan_result"])

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, msgs, config=None, **kwargs):
        return self.state["executor_result"]

    async def astream(self, _prompt, config=None, **kwargs):
        yield _FakeMsg("测试回答")


@pytest.fixture
def no_llm(monkeypatch):
    """拦截两个 LLM 工厂 + rag_tools._get_llm,返回可控替身。"""
    import lawApp_LangGraph.LangGraph_lawApp as app

    state = {"plan_result": _FakeVerdict(), "executor_result": None}

    monkeypatch.setattr(app, "get_planner_llm", lambda: _FakeLLM(state))
    monkeypatch.setattr(app, "get_executor_llm", lambda: _FakeLLM(state))

    import lawApp_LangGraph.tools.rag_tools as rag_tools

    class _ToolLLM:
        async def astream(self, _prompt):
            yield _FakeMsg("分析结果:测试回答")

    monkeypatch.setattr(rag_tools, "_get_llm", lambda: _ToolLLM())
    return state


#  1. import 冒烟


def test_imports():
    import lawApp_LangGraph.state  # noqa: F401
    import lawApp_LangGraph.tools  # noqa: F401
    import lawApp_LangGraph.db  # noqa: F401
    import lawApp_LangGraph.runtime  # noqa: F401
    import lawApp_LangGraph.LangGraph_lawApp  # noqa: F401
    import lawApp_LangGraph.FastAPI.api  # noqa: F401
    import lawApp_LangGraph.RAG_service.base  # noqa: F401


def test_state_reducers():
    from lawApp_LangGraph.state import RESET, append_list

    assert append_list([1], [2, 3]) == [1, 2, 3]
    assert append_list([1], RESET) == []
    assert append_list(None, [4]) == [4]
    # RESET 为字符串标记(必须可过 checkpointer 的 msgpack 序列化)
    assert isinstance(RESET, str)


def test_tools_registry():
    from lawApp_LangGraph.tools import ALL_TOOLS

    names = {t.name for t in ALL_TOOLS}
    assert {
        "search_memory", "save_to_memory", "fetch_laws", "get_google_search",
        "markdown_to_pdf", "retrieve_legal_knowledge",
        "evaluate_case_relevance", "analyze_legal_issue",
    } == names


def test_spelling_fixed():
    """拼写修正回归: 全仓 .py 中旧拼写零残留。"""
    import inspect

    from lawApp_LangGraph import state as st

    code = "\n".join(
        line for line in inspect.getsource(st).splitlines()
        if not line.lstrip().startswith(("#", "*"))
    )
    assert "evaluate_retrieved_documents" in code

    out = subprocess.run(
        ["grep", "-rl", "evluate_retrieved_documents", str(ROOT / "lawApp_LangGraph"),
         "--include=*.py"],
        capture_output=True, text=True,
    )
    assert not out.stdout.strip(), f"旧拼写残留: {out.stdout}"


#  2. 图构建


def test_graph_topology():
    import lawApp_LangGraph.LangGraph_lawApp as app

    g = app.build_graph()
    nodes = set(g.get_graph().nodes.keys())
    expected = {
        "ingest", "clarify", "planner", "executor", "tools", "merge",
        "replan_check", "replanner", "finalize",
    }
    assert expected <= nodes, f"missing: {expected - nodes}"


def test_runtime_inmemory_backend():
    import lawApp_LangGraph.runtime as rt

    async def run():
        await rt.setup_runtime()
        try:
            assert rt.checkpoint_backend in ("inmemory", "postgres")
            assert rt.graph is not None
        finally:
            await rt.teardown_runtime()

    asyncio.run(run())


#  3. 空计划 → finalize 直接回答


def test_chitchat_short_circuit(no_llm):
    import lawApp_LangGraph.LangGraph_lawApp as app

    no_llm["plan_result"] = _FakeVerdict(reasoning=["闲聊"], plan=[])

    async def run():
        g = app.build_graph()
        result = await g.ainvoke({"query": "你好"})
        assert result["final_answer"] == "测试回答"
        assert result["reasoning"] == ["闲聊"]

    asyncio.run(run())


#  4. HITL interrupt → resume(真实 clarify 节点)


def test_interrupt_resume(no_llm):
    """clarify 判定需反问 → interrupt 暂停 → Command(resume=用户补充) 恢复后完成回答."""
    import lawApp_LangGraph.LangGraph_lawApp as lg
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.types import Command

    no_llm["plan_result"] = _FakeVerdict(
        need_clarification=True, question="请问结婚多少年了?", plan=[]
    )

    async def run():
        g = lg.build_graph(checkpointer=MemorySaver())
        cfg = {"configurable": {"thread_id": "t-hitl"}}
        result = await g.ainvoke({"query": "我想离婚"}, config=cfg)
        assert not result.get("final_answer")

        snap = await g.aget_state(cfg)
        assert snap.next  # 图停在 interrupt,有待执行节点
        intr = next(iter(snap.interrupts), None)
        assert intr and intr.value["type"] == "clarify"
        assert intr.value["question"] == "请问结婚多少年了?"

        result2 = await g.ainvoke(Command(resume="结婚5年,有个3岁孩子"), config=cfg)
        assert result2.get("final_answer") == "测试回答"
        # 反问答案被织入增强 query,继续走 planner
        assert "用户补充信息" in result2["query"]

    asyncio.run(run())


#  5. 重启续聊: 同 thread_id 跨图实例的状态保持 + 请求间字段重置


def test_restart_same_thread(no_llm):
    import lawApp_LangGraph.LangGraph_lawApp as lg
    from langgraph.checkpoint.memory import MemorySaver

    no_llm["plan_result"] = _FakeVerdict(plan=[])

    async def run():
        checkpointer = MemorySaver()
        cfg = {"configurable": {"thread_id": "t-restart"}}
        g1 = lg.build_graph(checkpointer=checkpointer)
        r1 = await g1.ainvoke({"query": "你好"}, config=cfg)
        assert r1["final_answer"] == "测试回答"

        # 「重启」: 新图实例、同一 checkpointer → 历史可读
        g2 = lg.build_graph(checkpointer=checkpointer)
        snap = await g2.aget_state(cfg)
        assert snap.values.get("final_answer") == "测试回答"

        # 第二轮请求: ingest 以 RESET 字符串清空累积字段,不泄漏上一轮
        r2 = await g2.ainvoke({"query": "第二个问题"}, config=cfg)
        assert r2["final_answer"] == "测试回答"
        assert r2["tool_calls"] == []

    asyncio.run(run())
