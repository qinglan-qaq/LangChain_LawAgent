"""
冒烟测试 — upgrade-v1 重构验收 (pytest)

覆盖:
    1. 全仓 import(依赖 1.x 栈可加载)
    2. State reducers(append / RESET 清空)
    3. 图构建(节点拓扑 + InMemory checkpointer/store 装配)
    4. 空计划 → finalize 直接回答(monkeypatch LLM)
    5. HITL: 要素循环 assess → ask_element interrupt → Command(resume) 恢复
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
    """通用 structured output 替身,同时满足 Risk / ElementAssessment /
    Plan / ReplanCheck / MidClarify 各 Schema 的属性形状
    (图内各节点按需读取各自属性)。"""

    def __init__(
        self,
        *,
        plan=(),
        reasoning=(),
        need_clarification=False,
        question="",
        high_risk=False,
        needs_replan=False,
        reason="",
        applicable=True,
        element_updates=(),
        na_keys=(),
        promote_keys=(),
        questions=(),
        done=False,
        insufficient_reason="none",
    ):
        self.plan = list(plan)
        self.reasoning = list(reasoning)
        self.need_clarification = need_clarification
        self.question = question
        self.high_risk = high_risk
        self.needs_replan = needs_replan
        self.reason = reason
        self.applicable = applicable
        self.element_updates = list(element_updates)
        self.na_keys = list(na_keys)
        self.promote_keys = list(promote_keys)
        self.questions = list(questions)
        self.done = done
        self.insufficient_reason = insufficient_reason


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
    import lawApp_LangGraph.db  # noqa: F401
    import lawApp_LangGraph.FastAPI.api  # noqa: F401
    import lawApp_LangGraph.LangGraph_lawApp  # noqa: F401
    import lawApp_LangGraph.RAG_service.base  # noqa: F401
    import lawApp_LangGraph.runtime  # noqa: F401
    import lawApp_LangGraph.state  # noqa: F401
    import lawApp_LangGraph.tools  # noqa: F401


def test_state_reducers():
    from lawApp_LangGraph.state import RESET, append_list

    assert append_list([1], [2, 3]) == [1, 2, 3]
    assert append_list([1], RESET) == []
    assert append_list(None, [4]) == [4]
    # RESET 为字符串标记(必须可过 checkpointer 的 msgpack 序列化)
    assert isinstance(RESET, str)


def test_case_elements_model():
    from lawApp_LangGraph.config import settings
    from lawApp_LangGraph.state import default_case_elements

    ce = default_case_elements()
    # 默认 7 要素，关键 3 个
    keys = [e.key for e in ce.elements]
    assert keys == [
        "marriage_status",
        "demand",
        "property",
        "children",
        "timeline",
        "evidence",
        "opposing_stance",
    ]
    assert [e.label for e in ce.elements if e.critical] == [
        "婚姻现状",
        "核心诉求",
        "主要财产与归属",
    ]
    assert len(ce.critical_missing()) == 3

    # mark_na / promote / update
    ce.mark_na(["evidence"])
    ce.promote(["timeline"])
    assert ce.elements[4].critical is True  # timeline 升关键
    assert ce.elements[5].status == "na"  # evidence 不适用
    ce.update("marriage_status", "在婚,分居中", by="assess")
    assert ce.elements[0].status == "known"
    assert ce.elements[0].value == "在婚,分居中"
    assert ce.elements[0].updated_by == "assess"

    # critical_missing 随更新收缩
    assert [e.key for e in ce.critical_missing()] == ["demand", "property", "timeline"]

    # digest: known 的进文本, missing/na 不进
    ce.update("property", "一套房,双方名下", by="assess")
    d = ce.digest()
    assert "婚姻现状" in d and "一套房,双方名下" in d
    assert "核心诉求" not in d

    assert settings.max_clarify_rounds == 5
    assert settings.error_streak_threshold == 2


def test_agent_state_new_fields():
    from lawApp_LangGraph.state import AgentState

    s = AgentState(query="我想离婚")
    for f in (
        "case_elements",
        "clarify_history",
        "pending_questions",
        "clarify_rounds",
        "error_streak",
        "mid_clarify_used",
        "budget_hitl_used",
        "degrade_used",
    ):
        assert hasattr(s, f), f"缺少新字段 {f}"
    assert s.clarify_rounds == 0 and s.error_streak == 0
    assert not (s.mid_clarify_used or s.budget_hitl_used or s.degrade_used)
    # 旧字段已删
    assert not hasattr(s, "clarification_round")
    assert not hasattr(s, "clarification")


def test_tools_registry():
    from lawApp_LangGraph.tools import ALL_TOOLS, MCP_TOOLS

    MCP_TOOLS.clear()  # MCP 测试可能注册过外部工具,隔离验证本地注册表
    names = {t.name for t in ALL_TOOLS()}
    assert {
        "search_memory",
        "save_to_memory",
        "fetch_laws",
        "get_google_search",
        "markdown_to_pdf",
        "retrieve_legal_knowledge",
        "evaluate_case_relevance",
        "analyze_legal_issue",
    } == names


def test_spelling_fixed():
    """拼写修正回归: 全仓 .py 中旧拼写零残留。"""
    import inspect

    from lawApp_LangGraph import state as st

    code = "\n".join(
        line
        for line in inspect.getsource(st).splitlines()
        if not line.lstrip().startswith(("#", "*"))
    )
    assert "evaluate_retrieved_documents" in code

    out = subprocess.run(
        [
            "grep",
            "-rl",
            "evluate_retrieved_documents",
            str(ROOT / "lawApp_LangGraph"),
            "--include=*.py",
        ],
        capture_output=True,
        text=True,
    )
    assert not out.stdout.strip(), f"旧拼写残留: {out.stdout}"


#  2. 图构建


def test_graph_topology():
    import lawApp_LangGraph.LangGraph_lawApp as app

    g = app.build_graph()
    nodes = set(g.get_graph().nodes.keys())
    expected = {
        "ingest",
        "risk_gate",
        "element_assess",
        "ask_element",
        "planner",
        "executor",
        "tools",
        "merge",
        "replan_check",
        "mid_clarify",
        "hitl_degrade",
        "hitl_budget",
        "replanner",
        "finalize",
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


#  4. HITL interrupt → resume(要素循环: assess → ask interrupt → resume 补充)


def test_interrupt_resume(no_llm):
    """要素循环: assess 判定缺关键要素 → ask interrupt → resume 补充 → 要素齐 → planner."""
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.types import Command

    import lawApp_LangGraph.LangGraph_lawApp as lg
    from lawApp_LangGraph.state import ElementQuestion

    # (1) 首轮: 缺 marriage_status, 生成反问
    no_llm["plan_result"] = _FakeVerdict(
        need_clarification=True,
        question="请问结婚多少年了?",
        applicable=True,
        done=False,
        questions=[
            ElementQuestion(key="marriage_status", question="请问结婚多少年了?")
        ],
        plan=[],
    )

    async def run():
        g = lg.build_graph(checkpointer=MemorySaver())
        cfg = {"configurable": {"thread_id": "t-hitl2"}}
        result = await g.ainvoke({"query": "我想离婚"}, config=cfg)
        assert not result.get("final_answer")

        snap = await g.aget_state(cfg)
        assert snap.next
        intr = next(iter(snap.interrupts), None)
        assert intr and intr.value["type"] == "clarify"
        assert "结婚多少年" in intr.value["question"]

        # (2) resume 补充 → assess 二轮(无新反问) → planner(空计划) → finalize
        no_llm["plan_result"] = _FakeVerdict(reasoning=["要素齐"], plan=[])
        result2 = await g.ainvoke(Command(resume="结婚5年,有个3岁孩子"), config=cfg)
        assert result2.get("final_answer") == "测试回答"
        # M8: HITL 补充不再改写 query, 原文进 user_supplements
        # ([用户补充信息] 标记仅存在于 prompt 组装视图 _query_with_supplements)
        assert result2["user_supplements"] == ["结婚5年,有个3岁孩子"]
        assert "结婚" not in result2["query"]
        assert result2["clarify_history"], "应记录澄清历史"
        assert result2["clarify_rounds"] == 1

    asyncio.run(run())


#  5. 重启续聊: 同 thread_id 跨图实例的状态保持 + 请求间字段重置


def test_restart_same_thread(no_llm):
    from langgraph.checkpoint.memory import MemorySaver

    import lawApp_LangGraph.LangGraph_lawApp as lg

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
