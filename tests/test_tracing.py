"""P1 — 装饰器核心单测(纯 Python, 无 PG/无真实 LLM)。"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lawApp_LangGraph.tracing import (  # noqa: E402
    RunContext,
    Span,
    set_run,
    traced,
)


def test_traced_async_records_span():
    run = set_run(RunContext(run_id="t:1", session_id="t", run_type="live_ask"))

    @traced("node")
    async def planner_node(state, config=None):
        await asyncio.sleep(0.01)
        return {"plan": []}

    out = asyncio.run(planner_node({"query": "q"}, {"a": 1}))
    assert out == {"plan": []}
    assert len(run.spans) == 1
    s = run.spans[0]
    assert (s.span_type, s.name, s.status) == ("node", "planner_node", "ok")
    assert s.latency_ms >= 5  # sleep 10ms
    assert s.input == {"args": [{"query": "q"}, {"a": 1}], "kwargs": {}}  # 双参打包
    assert s.output == {"plan": []}  # 前后成果: 返回值


def test_traced_single_positional_arg_packed_raw():
    run = set_run(RunContext(run_id="t:1b", session_id="t", run_type="live_ask"))

    @traced("tool")
    async def fetch_laws(query):
        return {"ok": True}

    asyncio.run(fetch_laws("q"))
    assert run.spans[0].input == "q"  # 单位置参原样(前后成果: 入参)


def test_traced_exception_passthrough_and_error_span():
    run = set_run(RunContext(run_id="t:2", session_id="t", run_type="live_ask"))

    @traced("tool")
    async def boom(x):
        raise ValueError("业务异常必须透传")

    try:
        asyncio.run(boom(1))
        raise AssertionError("必须抛出")
    except ValueError:
        pass
    s = run.spans[0]
    assert s.status == "error"
    assert "业务异常必须透传" in s.output["exception"]


def test_traced_sync_function():
    run = set_run(RunContext(run_id="t:3", session_id="t", run_type="live_ask"))

    @traced("tool")
    def evaluate_case_relevance(query, docs=None):
        return {"applicable": True}

    assert evaluate_case_relevance("q") == {"applicable": True}
    assert run.spans[0].name == "evaluate_case_relevance"
    assert run.spans[0].span_type == "tool"


def test_orphan_buffer_no_run_context():
    # 无 run 上下文(单测直接调被装饰函数): 进游离缓冲, 不报错不落库
    from lawApp_LangGraph import tracing

    # 前序测试在 pytest 主上下文 set 的 run 仍存活 → 先清再验游离路径
    tracing._current_run.set(None)

    @traced("node")
    async def free_node():
        return 1

    asyncio.run(free_node())
    assert tracing._ORPHAN_SPANS[-1].name == "free_node"


def test_metrics_aggregation():
    run = set_run(RunContext(run_id="t:5", session_id="t", run_type="live_ask"))
    run.add(Span(span_type="node", name="planner", latency_ms=100,
                token_usage={"prompt": 10, "completion": 5}))
    run.add(Span(span_type="tool", name="retrieve_legal_knowledge", latency_ms=50))
    run.add(Span(span_type="llm", name="llm:deepseek-chat", latency_ms=30,
                token_usage={"prompt": 100, "completion": 50}))
    run.add(Span(span_type="node", name="ask_element", latency_ms=5))
    run.add(Span(span_type="node", name="mid_clarify", latency_ms=5))
    m = run.metrics()
    assert m == {
        "node_count": 3, "tool_count": 1, "llm_count": 1,
        "total_latency_ms": 190, "token_prompt": 110,
        "token_completion": 55, "clarify_rounds": 2,
    }


def test_attach_state_backfills_node_spans():
    run = set_run(RunContext(run_id="t:6", session_id="t", run_type="live_ask"))
    run.add(Span(span_type="node", name="planner"))
    run.add(Span(span_type="node", name="planner"))  # 二次执行同节点
    run.add(Span(span_type="tool", name="retrieve_legal_knowledge"))
    from lawApp_LangGraph.tracing import attach_state

    attach_state(["planner"], {"plan": ["s1"]})
    # 两个 planner span 都回填, tool span 不动
    assert run.spans[0].state == {"plan": ["s1"]}
    assert run.spans[1].state == {"plan": ["s1"]}
    assert run.spans[2].state is None
    # 已回填的不覆盖
    attach_state(["planner"], {"plan": ["s2"]})
    assert run.spans[0].state == {"plan": ["s1"]}
