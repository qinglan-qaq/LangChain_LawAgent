"""边界审查中危批(M1-M15)回归测试。

不依赖真 PG / DeepSeek / MCP server / Pinecone —— 全部 stub/monkeypatch;
不碰存量测试文件。覆盖:
    M1  pgvector rerank/top_k 钳制(0/负数不再假"未检索到")
    M2  planner 裸 openai 客户端单例 + timeout=60
    M3  replanner 空 plan → finalize 收尾(不触 recursion_limit)
    M4  semantic_confirm 失败降级短词精确匹配(不裸 500 悬死 interrupt)
    M5  客户端断开 trace 记 cancelled(api._run_sse / tracing.traced / llm _astream)
    M6  session id uuid 化 + 格式校验(非法 400)
    M7  MCP PG store 失败不静默换 InMemory(记忆工具显式报错)
    M8  HITL 补充进 user_supplements(query 不再改写)+ prompt 拼接视图
    M9  工具异常原文不进对话(固定中文文案)
    M10 LLM 空 answer → status error
    M11 POST /assistant/ask/stream(body 传参, 事件协议与 GET 一致)
    M13 AT-/AS- 前缀与端点模式不符 → 400 session_mode_mismatch
    M14 db pool 加锁防双建 / 失败不缓存坏池 / timeout=5
    M15 aget_state 失败降级返回(不 500)
"""
import asyncio
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _run(coro):
    return asyncio.run(coro)


#  环境快照(模块导入时, 先于任何 api/config 导入): api.py 模块级 load_dotenv
#  会把 lawApp_LangGraph/.env(DB_PORT=15432 等)注入 os.environ, 污染同进程
#  后续文件(如 test_engineering 的默认值断言)→ teardown 清掉新增键
_ENV_KEYS = frozenset(os.environ)


#  模块级单例清理(防串测试;asyncio.Lock 绑定单事件循环 → 相关用例在
#  单个 asyncio.run 内完成全部调用)
@pytest.fixture(autouse=True)
def _reset_module_globals():
    yield
    import lawApp_LangGraph.FastAPI.api as api
    import lawApp_LangGraph.LangGraph_lawApp as app
    import lawApp_LangGraph.db as db

    api._SESSION_LOCKS.clear()
    app._REASONING_BUS.clear()
    app._planner_openai_client = None  # M2 单例
    db._pool = None  # M14 池引用
    mcp_srv = sys.modules.get("lawApp_LangGraph.mcp.mcp_server")
    if mcp_srv is not None:
        mcp_srv._store = None
    for k in set(os.environ) - set(_ENV_KEYS):
        if k != "PYTEST_CURRENT_TEST":  # pytest 自身管理, 有删除竞态
            os.environ.pop(k, None)


# ── M1: pgvector 参数钳制 ──


def test_m1_clamp_search_params_bounds():
    """0/负数/超大/非数 → 1..50 / 1..10 内的合法值。"""
    from lawApp_LangGraph.RAG_service.pgvector_retriever import clamp_search_params

    assert clamp_search_params(20, 5) == (20, 5)
    assert clamp_search_params(0, 0) == (1, 1)
    assert clamp_search_params(-5, -3) == (1, 1)
    assert clamp_search_params(1000, 99) == (50, 10)
    assert clamp_search_params("abc", None) == (20, 5)


def test_m1_rerank_zero_still_returns_rows(monkeypatch):
    """rerank_top_n=0 不再把切片切成空(旧实现返回 [] 假装"未检索到")。"""
    import lawApp_LangGraph.RAG_service.pgvector_retriever as pvr
    import lawApp_LangGraph.db as db

    rows = [
        (1, "2020", "(2020)京01号", "离婚", "正文A", 0.87),
        (2, "2022", "(2022)粤03号", "抚养", "正文C", 0.52),
    ]

    class _Cur:
        def __init__(self, r):
            self._r = r

        async def fetchall(self):
            return self._r

    class _Conn:
        async def execute(self, sql, params=None):
            return _Cur(rows)

    class _CM:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *a):
            return False

    class _Pool:
        def connection(self):
            return _CM()

    async def _fake_embed_query(q):
        return [0.1] * 8

    async def _fake_rerank(q, docs, **k):
        return None  # 禁用重排, 走召回排序切片分支

    async def _fake_get_pool():
        return _Pool()

    monkeypatch.setattr(pvr, "embed_query", _fake_embed_query)
    monkeypatch.setattr(pvr, "rerank", _fake_rerank)
    monkeypatch.setattr(db, "get_pool", _fake_get_pool)

    # 0 钳制为 1: 仍返回最高分一条, 而非旧实现的空列表
    results = _run(pvr.PgvectorRetriever().search("测试", top_k=2, rerank_top_n=0))
    assert len(results) == 1, "rerank_top_n=0 必须按钳制值返回, 不允许返回空列表"
    assert results[0]["id"] == 1
    assert results[0]["rank"] == 1


# ── M2: planner 裸 openai 客户端单例 + 超时 ──


def test_m2_planner_openai_client_singleton_with_timeout(monkeypatch):
    """两次获取同一实例;构造带 timeout=60(不再每次 _stream_plan 新建泄漏)。"""
    import openai

    import lawApp_LangGraph.LangGraph_lawApp as app

    captured: list[dict] = []

    class _FakeAsyncOpenAI:
        def __init__(self, **kw):
            captured.append(kw)

    # _get_planner_openai_client 函数内 import openai → 打模块属性即生效
    monkeypatch.setattr(openai, "AsyncOpenAI", _FakeAsyncOpenAI)
    app._planner_openai_client = None

    c1 = app._get_planner_openai_client()
    c2 = app._get_planner_openai_client()
    assert c1 is c2, "必须复用模块级单例"
    assert len(captured) == 1, "两次调用只允许构造一次客户端"
    assert captured[0]["timeout"] == 60.0


# ── M3: replanner 空 plan → finalize ──


def _fake_executor_llm():
    """让所有 _structured 决策链构建即失败(PromptTemplate | 非Runnable →
    TypeError), 由 risk_gate/element_assess/replan_check 的 except 分支接管,
    不触真实 API key。"""

    class _NotRunnable:
        def with_structured_output(self, schema):
            return self

    return _NotRunnable()


def test_m3_replanner_empty_plan_reaches_finalize(monkeypatch):
    """planner 给一步无工具计划 → replan_check 判不足 → replanner 返回空
    plan → 必须直接 finalize 收尾, 不再 executor 空转到 recursion_limit。"""
    from langgraph.checkpoint.memory import MemorySaver

    import lawApp_LangGraph.LangGraph_lawApp as app

    async def _fake_stream_plan(prompt_text, source, config):
        if source == "planner":
            plan = app.PlanSchema.model_validate(
                {
                    "reasoning": [],
                    "plan": [
                        {"step_id": 1, "description": "直接分析", "tool_name": None}
                    ],
                }
            )
        else:  # replanner → 空 plan(M3 场景)
            plan = app.PlanSchema.model_validate({"reasoning": [], "plan": []})
        return plan, []

    def _fallback(state):
        return True, "测试: 需要重规划", "not_found"

    finalize_calls: list = []

    async def _fake_finalize(state, config=None):
        finalize_calls.append(state)
        return {"final_answer": "已按现有材料收尾"}

    monkeypatch.setattr(app, "_stream_plan", _fake_stream_plan)
    monkeypatch.setattr(app, "get_executor_llm", _fake_executor_llm)
    monkeypatch.setattr(app, "_fallback_replan_check", _fallback)
    # 节点在 build 时绑定 → 必须先 patch 再 build
    monkeypatch.setattr(app, "finalize_node", _fake_finalize)

    graph = app.build_graph(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "T-M3-1"}, "recursion_limit": 30}
    # 旧实现此场景循环空转直至 GraphRecursionError; 修复后少量超步收尾
    result = _run(
        graph.ainvoke({"query": "测试问题", "mode": "attorney"}, config=config)
    )

    assert finalize_calls, "空补充计划必须路由到 finalize"
    assert result.get("replan_empty") is True
    assert result.get("final_answer") == "已按现有材料收尾"


def test_m3_replan_check_empty_plan_guard():
    """replan_check 兜底: 空 plan 不再触发 replanner(直接放行 finalize)。"""
    import lawApp_LangGraph.LangGraph_lawApp as app
    from lawApp_LangGraph.state import AgentState

    state = AgentState(query="测试问题", plan=[])
    out = _run(app.replan_check_node(state, None))
    assert out["replan_needed"] is False


def test_m3_route_after_replanner():
    from lawApp_LangGraph.LangGraph_lawApp import route_after_replanner

    assert route_after_replanner(types.SimpleNamespace(replan_empty=True)) == "finalize"
    assert (
        route_after_replanner(types.SimpleNamespace(replan_empty=False)) == "executor"
    )


# ── M4: semantic_confirm 失败降级 ──


def _patch_normalize_resume(monkeypatch, impl):
    import lawApp_LangGraph.FastAPI.api as api

    monkeypatch.setattr(api, "normalize_resume", impl)


def test_m4_llm_failure_degrades_to_exact_word_match(monkeypatch):
    """normalize_resume 抛错 → 降级短词精确匹配, 不再裸 500 悬死 interrupt。"""
    import lawApp_LangGraph.FastAPI.api as api

    async def _boom(itype, answer, request_text):
        raise RuntimeError("LLM 语义判断失败(模拟)")

    _patch_normalize_resume(monkeypatch, _boom)

    d = api._normalize_resume_with_degrade
    # risk/pdf: 确认词 → True; 拒绝词 → False; 自由文本默认拒绝(abort)
    assert _run(d("risk_confirm", "是", "m")) is True
    assert _run(d("pdf_confirm", "跳过", "m")) is False
    assert _run(d("risk_confirm", "我不太确定", "m")) is False
    # degrade: 指令词直接命中; 自由文本默认 abort
    assert _run(d("degrade_confirm", "重试", "m")) == "retry"
    assert _run(d("degrade_confirm", "跳过", "m")) == "skip"
    assert _run(d("degrade_confirm", "终止", "m")) == "abort"
    assert _run(d("degrade_confirm", "随便说说", "m")) == "abort"
    # budget: 空/收尾 → finish; continue → 原文; 自由文本默认 finish
    assert _run(d("budget_confirm", "", "m")) == "finish"
    assert _run(d("budget_confirm", "收尾", "m")) == "finish"
    assert _run(d("budget_confirm", "继续", "m")) == "继续"
    assert _run(d("budget_confirm", "我再补两句", "m")) == "finish"
    # clarify: 原文透传
    assert _run(d("clarify", "结婚三年", "m")) == "结婚三年"


def test_m4_success_path_passthrough(monkeypatch):
    """normalize_resume 正常时降级包装不改变其返回值。"""
    import lawApp_LangGraph.FastAPI.api as api

    async def _ok(itype, answer, request_text):
        return "正常值"

    _patch_normalize_resume(monkeypatch, _ok)
    assert (
        _run(api._normalize_resume_with_degrade("budget_confirm", "x", "m"))
        == "正常值"
    )


def test_m4_resume_endpoint_survives_semantic_confirm_failure(monkeypatch):
    """端点级: LLM 语义判断失败时 resume 仍走完(200), interrupt 不悬死。"""
    from fastapi.testclient import TestClient

    import lawApp_LangGraph.FastAPI.api as api

    async def _boom(itype, answer, request_text):
        raise RuntimeError("LLM down(模拟)")

    _patch_normalize_resume(monkeypatch, _boom)

    class _Snap:
        def __init__(self, interrupt_req):
            intr = (
                types.SimpleNamespace(value=interrupt_req) if interrupt_req else None
            )
            self.interrupts = [intr] if intr else []
            self.values = {}

    class _FakeGraph:
        """aget_state: budget interrupt 常驻;ainvoke: 恢复并返回终态。"""

        async def aget_state(self, config):
            return _Snap({"type": "budget_confirm", "message": "是否补充?"})

        async def ainvoke(self, cmd, config=None):
            return {"query": "q", "final_answer": "降级后仍完成", "reasoning": []}

    async def _no_audit(*a, **k):
        return None

    monkeypatch.setattr(api, "get_graph", lambda: _FakeGraph())
    monkeypatch.setattr(api, "_safe_audit", _no_audit)

    client = TestClient(api.app)
    r = client.post("/ask/resume", json={"session_id": "T-M4-1", "answer": "继续"})
    assert r.status_code == 200, r.text
    assert r.json()["final_answer"] == "降级后仍完成"


# ── M5: 断开 trace 记 cancelled ──


def test_m5_traced_cancelled_span():
    """@traced 的 async 包装: CancelledError → span status=cancelled 并 re-raise。"""
    import lawApp_LangGraph.tracing as tracing

    @tracing.traced("node", "m5_node")
    async def _cancelled_node():
        raise asyncio.CancelledError()

    run = tracing.set_run(
        tracing.RunContext(run_id="t", session_id="t", run_type="eval")
    )
    with pytest.raises(asyncio.CancelledError):
        _run(_cancelled_node())
    spans = [s for s in run.spans if s.name == "m5_node"]
    assert spans and spans[0].status == "cancelled"


def test_m5_llm_astream_closed_early_emits_cancelled_span(monkeypatch):
    """InstrumentedChatOpenAI._astream 生成器被提前 close → 已聚合部分照落
    span 且 status=cancelled(旧实现 yield 点的 GeneratorExit 不是 Exception,
    span 直接丢失)。"""
    from langchain_openai import ChatOpenAI

    import lawApp_LangGraph.tracing as tracing

    class _Chunk:
        def __init__(self, content):
            self.content = content

    async def _fake_super_astream(self, messages, stop=None, run_manager=None, **kw):
        yield _Chunk("部分答案")
        yield _Chunk("永远到不了")

    monkeypatch.setattr(ChatOpenAI, "_astream", _fake_super_astream)

    cls = tracing.get_instrumented_llm_cls()
    inst = cls.__new__(cls)  # 跳过 ChatOpenAI 初始化(不触 API key)
    object.__setattr__(inst, "model_name", "fake-model")

    run = tracing.set_run(
        tracing.RunContext(run_id="t", session_id="t", run_type="eval")
    )

    async def _drive():
        agen = inst._astream([{"role": "user", "content": "q"}])
        first = await agen.__anext__()
        assert getattr(first, "content", "") == "部分答案"
        await agen.aclose()  # 客户端断开 → yield 点 GeneratorExit

    _run(_drive())
    llm_spans = [s for s in run.spans if s.span_type == "llm"]
    assert llm_spans, "生成器提前 close 也必须落 llm span"
    assert llm_spans[0].status == "cancelled"
    assert llm_spans[0].output == "部分答案", "已聚合的部分要照常落 span"


def test_m5_run_sse_disconnect_records_cancelled(monkeypatch):
    """_run_sse: 客户端断开(消费被取消)→ flush_run 收到 status=cancelled,
    不再留 "ok"。"""
    import lawApp_LangGraph.FastAPI.api as api

    class _HangGraph:
        async def astream(self, inp, config=None, stream_mode=None):
            await asyncio.sleep(3600)  # 挂死流, 模拟长回答中客户端断开
            yield  # pragma: no cover — 使其成为 async generator

    flushed: list = []

    async def _capture_flush(run):
        flushed.append(run)

    async def _no_audit(*a, **k):
        return None

    monkeypatch.setattr(api, "get_graph", lambda: _HangGraph())
    monkeypatch.setattr(api, "flush_run", _capture_flush)
    monkeypatch.setattr(api, "_safe_audit", _no_audit)

    async def _drive():
        resp = api._run_sse("T-M5-1", {"query": "q"}, mode="attorney", session_lock=None)
        it = resp.body_iterator
        starter = asyncio.ensure_future(it.__anext__())
        await asyncio.sleep(0.3)  # 让生成器跑到首个 await(out_q.get)
        starter.cancel()  # 模拟客户端断开: 消费侧被取消
        try:
            await starter
        except asyncio.CancelledError:
            pass
        # 等子任务(run/pump/ping)的 finally 全部执行完
        for _ in range(20):
            await asyncio.sleep(0.05)

    _run(_drive())
    assert flushed, "断开路径必须 flush trace"
    assert flushed[-1].status == "cancelled", "trace 状态必须是 cancelled 而非 ok"


# ── M6/M13: session id 模式-时间-编号 + 校验 ──
# M6 规格随用户决策变更: uuid → 模式-时间-编号(uuid 仍兼容)


def test_m6_new_session_id_timestamp_format_and_uniqueness():
    import re

    from lawApp_LangGraph.FastAPI.utils import new_session_id

    ids = {new_session_id("attorney") for _ in range(50)}
    assert len(ids) == 50, "基本唯一性: 50 次生成不得撞号(同秒靠编号递增防重)"
    pat = re.compile(r"^AT-\d{8}-\d{6}-\d{3}$")
    for sid in ids:
        assert pat.match(sid), f"格式必须为 模式-时间-编号(3位): {sid}"
    # 同秒两次生成 → 编号不同(进程内锁+计数)
    assert new_session_id("attorney") != new_session_id("attorney")
    assert re.match(r"^AS-\d{8}-\d{6}-\d{3}$", new_session_id("assistant"))


def test_m6_ensure_session_validation():
    from fastapi import HTTPException

    from lawApp_LangGraph.FastAPI.utils import ensure_session

    # 现行 模式-时间-编号 格式
    assert ensure_session("AT-20260918-143025-001", "attorney") == "AT-20260918-143025-001"
    assert ensure_session("AS-20260918-143025-42", "assistant") == "AS-20260918-143025-42"
    # 存量 uuid 格式(旧会话兼容)
    assert ensure_session("AT-1a2b3c4d5e6f", "attorney") == "AT-1a2b3c4d5e6f"
    assert ensure_session("AS-1a2b3c4d5e6f", "assistant") == "AS-1a2b3c4d5e6f"
    # 无前缀历史 sid: 放行
    assert ensure_session("T-409-1", "attorney") == "T-409-1"
    # 空 sid → 新建
    assert ensure_session(None, "attorney").startswith("AT-")
    assert ensure_session("   ", "attorney").startswith("AT-")
    # 带前缀但格式非法 → 400 invalid_session_id
    with pytest.raises(HTTPException) as ei:
        ensure_session("AT-###非法###", "attorney")
    assert ei.value.status_code == 400
    assert ei.value.detail == "invalid_session_id"
    # 前缀与模式不符 → 400 session_mode_mismatch(M13)
    with pytest.raises(HTTPException) as ei:
        ensure_session("AS-1a2b3c4d5e6f", "attorney")
    assert ei.value.status_code == 400
    assert ei.value.detail == "session_mode_mismatch"


def test_m6_endpoint_rejects_bad_session_id():
    from fastapi.testclient import TestClient

    import lawApp_LangGraph.FastAPI.api as api

    client = TestClient(api.app)
    r = client.post(
        "/attorney/ask", json={"query": "测试", "session_id": "AT-垃圾格式"}
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_session_id"


def test_m13_endpoint_rejects_cross_mode_session_id():
    """assistant 的 AS- 前缀 sid 打 attorney 端点 → 400 session_mode_mismatch。"""
    from fastapi.testclient import TestClient

    import lawApp_LangGraph.FastAPI.api as api

    client = TestClient(api.app)
    r = client.post(
        "/attorney/ask", json={"query": "测试", "session_id": "AS-1a2b3c4d5e6f"}
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "session_mode_mismatch"


# ── M7: MCP PG store 失败不静默换 InMemory ──


def _import_mcp_server(monkeypatch):
    """导入 mcp_server(避开 _preload_native_deps 的 ~1GB 模型加载)。"""
    import lawApp_LangGraph.RAG_service.embedder as emb

    monkeypatch.setattr(emb, "get_embedder", lambda: object())
    import lawApp_LangGraph.mcp.mcp_server as srv

    srv._store = None
    return srv


def test_m7_store_failure_memory_tool_returns_error(monkeypatch):
    """PG store setup 失败 → recall_memory 显式报"记忆库暂不可用",
    不静默挂 InMemory(图写 PG / server 读内存互不可见)。"""
    srv = _import_mcp_server(monkeypatch)

    import langgraph.store.postgres.aio as aio_store

    constructions: list[int] = []

    class _FailingStore:
        def __init__(self, **kw):
            constructions.append(1)
            self.conn = None

        async def setup(self):
            raise RuntimeError("PG 不可用(模拟)")

    # _get_server_store 函数内 from ...aio import AsyncPostgresStore
    # → 打 aio 模块属性即生效
    monkeypatch.setattr(aio_store, "AsyncPostgresStore", _FailingStore)

    async def _drive():
        out = await srv.recall_memory("测试记忆")
        assert "记忆库暂不可用" in out
        # 失败可重试(再次构造), 而非缓存失败或静默换 InMemory
        out2 = await srv.recall_memory("测试记忆")
        assert "记忆库暂不可用" in out2

    _run(_drive())
    assert srv._store is None, "不允许静默挂 InMemory 或缓存失败实例"
    assert len(constructions) == 2, "失败不缓存, 下次调用应重试 PG"


# ── M8: user_supplements ──


def test_m8_hitl_answers_accumulate_in_supplements_not_query(monkeypatch):
    """两轮 HITL 补充后: 不再改写 query, user_supplements 累积。"""
    import lawApp_LangGraph.LangGraph_lawApp as app

    answers = iter(["第一次补充", "第二次补充"])
    monkeypatch.setattr(app, "interrupt", lambda payload: next(answers))

    state = types.SimpleNamespace(
        evaluation=None, rag_documents=[], error=None, query="原始问题",
        user_supplements=["前置补充"],
    )
    out1 = app.hitl_budget_node(state)
    assert "query" not in out1, "query 不再被改写"
    assert out1["user_supplements"] == ["前置补充", "第一次补充"]

    state2 = types.SimpleNamespace(
        evaluation=None, rag_documents=[], error=None, query="原始问题",
        user_supplements=out1["user_supplements"],
    )
    out2 = app.hitl_budget_node(state2)
    assert out2["user_supplements"] == ["前置补充", "第一次补充", "第二次补充"]


def test_m8_query_with_supplements_view_and_limits():
    """补充视图: 原文截断后拼接; 每条 500 字、总量 2000 字封顶。"""
    from lawApp_LangGraph.LangGraph_lawApp import (
        _SUPPLEMENT_PER_ITEM_LIMIT,
        _SUPPLEMENT_TOTAL_LIMIT,
        _query_with_supplements,
    )

    state = types.SimpleNamespace(
        query="Q" * 4000, user_supplements=["补" * 600, "二" * 50]
    )
    view = _query_with_supplements(state, 3000)
    assert view.startswith("Q" * 3000), "原 query 按传入上限截断"
    assert "[用户补充信息] " + "补" * _SUPPLEMENT_PER_ITEM_LIMIT in view, "每条截 500 字"
    assert "二" * 50 in view
    assert len(view) <= 3000 + _SUPPLEMENT_TOTAL_LIMIT + 100

    # 总量上限: 5 条 × 500 超 2000 → 只保留前 4 条
    state2 = types.SimpleNamespace(
        query="q", user_supplements=["A" * 500 for _ in range(5)]
    )
    view2 = _query_with_supplements(state2)
    assert view2.count("[用户补充信息] ") == 4, "超出 2000 字上限后截断"
    assert "A" * 500 in view2


def test_m8_planner_prompt_includes_supplements(monkeypatch):
    """planner 组 prompt 时带上补充内容(M8: HITL 答案拼进规划视图)。"""
    import lawApp_LangGraph.LangGraph_lawApp as app

    captured: list[str] = []

    async def _fake_stream_plan(prompt_text, source, config):
        captured.append(prompt_text)
        plan = app.PlanSchema.model_validate({"reasoning": [], "plan": []})
        return plan, []

    monkeypatch.setattr(app, "_stream_plan", _fake_stream_plan)

    state = app.AgentState(query="原始问题", user_supplements=["结婚三年有一子"])
    _run(app.planner_node(state, {"configurable": {"thread_id": "T-M8"}}))
    assert captured, "planner 必须组装 prompt"
    assert "原始问题" in captured[0]
    assert "[用户补充信息] 结婚三年有一子" in captured[0], "补充内容必须进 planner prompt"


# ── M9: 异常原文不进对话 ──


def test_m9_web_search_error_fixed_message(monkeypatch):
    """SerpAPI 异常 → 固定中文文案, 原始异常串不进工具结果。"""
    import langchain_community.utilities as lc_utils

    class _BadSerp:
        def results(self, query):
            raise RuntimeError("connect timeout to https://serpapi.example?key=SECRET")

    monkeypatch.setattr(lc_utils, "SerpAPIWrapper", _BadSerp)

    from lawApp_LangGraph.tools.tools import get_google_search

    out = _run(get_google_search.ainvoke({"query": "测试"}))
    assert out["status"] == "error"
    assert out["message"] == "联网搜索暂时不可用,请稍后重试"
    assert "SECRET" not in out["message"] and "serpapi" not in out["message"]


def test_m9_rag_backend_error_fixed_message(monkeypatch):
    """检索后端异常 → 固定中文文案, DSN/异常原文不进工具结果。"""
    import lawApp_LangGraph.RAG_service.base as rag_base

    def _get_retriever():
        raise RuntimeError("connection to postgresql://user:pwd@10.0.0.1 refused")

    monkeypatch.setattr(rag_base, "get_retriever", _get_retriever)

    from lawApp_LangGraph.tools.rag_tools import retrieve_legal_knowledge

    out = _run(retrieve_legal_knowledge.ainvoke({"query": "测试"}))
    assert out["status"] == "error"
    assert out["message"] == "案例检索暂时不可用,请稍后重试"
    assert "pwd" not in out["message"]


# ── M10: 空 LLM 流 → error ──


def test_m10_empty_answer_returns_error(monkeypatch):
    """LLM 流读尽但 answer 为空 → status=error, 不再当成功返回空 final_answer。"""
    import lawApp_LangGraph.tools.rag_tools as rag

    class _EmptyLLM:
        def astream(self, prompt):
            async def _gen():
                yield types.SimpleNamespace(content="")

            return _gen()

    monkeypatch.setattr(rag, "_get_llm", lambda: _EmptyLLM())

    out = _run(rag.analyze_legal_issue.ainvoke({"query": "测试", "prompts_record": None}))
    assert out["status"] == "error"
    assert "分析结果为空" in out["message"]
    assert "final_answer" not in out


# ── M11: POST /assistant/ask/stream ──


def test_m11_post_assistant_stream_behaves_like_get(monkeypatch):
    """POST body 传参的 assistant 流: 与 GET 相同的事件协议(含 session_id/done)。"""
    from fastapi.testclient import TestClient

    import lawApp_LangGraph.FastAPI.api as api

    class _QuickGraph:
        async def astream(self, inp, config=None, stream_mode=None):
            return
            yield  # pragma: no cover — 使其成为 async generator

        async def aget_state(self, config):
            return types.SimpleNamespace(interrupts=[], values={})

    async def _no_flush(run):
        return None

    async def _no_audit(*a, **k):
        return None

    async def _no_upsert(sid):
        return None

    monkeypatch.setattr(api, "get_graph", lambda: _QuickGraph())
    monkeypatch.setattr(api, "flush_run", _no_flush)
    monkeypatch.setattr(api, "_safe_audit", _no_audit)
    monkeypatch.setattr(api, "_safe_upsert_session", _no_upsert)

    client = TestClient(api.app)
    r = client.post(
        "/assistant/ask/stream",
        json={"case_details": "案情详情" * 10, "doc_type": "defense"},
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    body = r.text
    assert '"event": "session_id"' in body
    assert '"event": "done"' in body
    # 新会话 id 为 AS- 前缀(assistant 模式)
    assert '"AS-' in body

    # 校验: 空 case_details → 422; 非法 doc_type → 422
    r2 = client.post(
        "/assistant/ask/stream", json={"case_details": "", "doc_type": "complaint"}
    )
    assert r2.status_code == 422
    r3 = client.post(
        "/assistant/ask/stream", json={"case_details": "x" * 30, "doc_type": "bad"}
    )
    assert r3.status_code == 422


# ── M14: db pool 竞态/坏池缓存 ──


def test_m14_pool_failure_not_cached_and_reuse(monkeypatch):
    """连接失败两次后成功: 失败不缓存坏池;成功后复用同一池; timeout=5 入参。"""
    import lawApp_LangGraph.db as db

    state = {"constructions": 0, "closed": [], "kwargs": None}

    class _FakeConn:
        async def execute(self, sql, params=None):
            return None

    class _CM:
        async def __aenter__(self):
            return _FakeConn()

        async def __aexit__(self, *a):
            return False

    class _FakePool:
        def __init__(self, **kw):
            state["constructions"] += 1
            state["kwargs"] = kw
            self.closed = False

        async def open(self, wait=False):
            if state["constructions"] <= 2:  # 前两次连接失败
                raise ConnectionError("PG 掉线(模拟)")

        async def close(self):
            state["closed"].append(self)
            self.closed = True

        def connection(self):
            return _CM()

    monkeypatch.setattr(db, "AsyncConnectionPool", _FakePool)

    async def _no_ensure_tables(conn):
        return None

    monkeypatch.setattr(db, "ensure_tables", _no_ensure_tables)
    db._pool = None

    async def _drive():
        # 前两次: 失败抛出且不缓存(_pool 保持 None)
        with pytest.raises(ConnectionError):
            await db.get_pool()
        assert db._pool is None, "失败状态不允许缓存坏池"
        with pytest.raises(ConnectionError):
            await db.get_pool()
        assert db._pool is None
        # 第三次成功
        pool = await db.get_pool()
        assert db._pool is pool
        # 已就绪时复用, 不再新建
        again = await db.get_pool()
        assert again is pool
        # close_pool: 先摘全局引用再关
        await db.close_pool()
        assert db._pool is None
        assert pool in state["closed"]

    _run(_drive())
    assert state["constructions"] == 3
    # timeout=5 已传入池构造(M14: 池操作挂死时快速失败)
    assert state["kwargs"].get("timeout") == 5


# ── M15: aget_state 失败降级返回 ──


def test_m15_finalize_or_interrupt_degrades_on_aget_state_failure(monkeypatch):
    """PG 掉线: 已跑完的结果不 500 —— 用 state_values 构造响应(不附 interrupt)。"""
    import lawApp_LangGraph.FastAPI.api as api

    class _BrokenGraph:
        async def aget_state(self, config):
            raise RuntimeError("PG 掉线(模拟)")

    async def _no_audit(*a, **k):
        return None

    monkeypatch.setattr(api, "get_graph", lambda: _BrokenGraph())
    monkeypatch.setattr(api, "_safe_audit", _no_audit)

    response = _run(
        api._finalize_or_interrupt("T-M15-1", {"query": "q", "final_answer": "完整答案"})
    )
    assert response.final_answer == "完整答案"
    assert response.interrupt is None


def test_m15_get_session_endpoint_degrades_to_200(monkeypatch):
    """GET /sessions/{sid}: aget_state 抛错 → 200 降级响应(interrupt=None)。"""
    from fastapi.testclient import TestClient

    import lawApp_LangGraph.FastAPI.api as api

    class _BrokenGraph:
        async def aget_state(self, config):
            raise RuntimeError("PG 掉线(模拟)")

    monkeypatch.setattr(api, "get_graph", lambda: _BrokenGraph())

    client = TestClient(api.app)
    r = client.get("/sessions/T-M15-2")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["session_id"] == "T-M15-2"
    assert body["interrupt"] is None
    assert body.get("degraded") is True
