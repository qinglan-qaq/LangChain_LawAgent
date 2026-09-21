"""边界审查低危批(L1-L22)回归测试。

不依赖真 PG / DeepSeek / MCP server / Pinecone —— 全部 stub/monkeypatch;
不碰存量测试文件(tests/ 其余 8 个文件不动, 高危/中危两个 boundary 文件也不改)。
覆盖:
    L1  _safe_audit 失败记 warning(caplog)+ /feedback 写失败 200 degraded
    L2  sse_event 序列化非 JSON 原生值 payload 不抛
    L3  runtime/mcp_server 嵌入维度接 settings.embed_dim(源码核对)
    L4  search_memory 降级 fallback 自身失败 → status:"error"
    L5  evaluate_case_relevance 收 dict(取不到 rag_documents/仍非列表)→ error
    L7  MCP search_cases top_k 透传钳制(源码核对)
    L8  health_check 轻量化(不触发检索/模型加载)
    L15 degrade 询问计数门槛(直调路由函数)
    L19 缺 DEEPSEEK_API_KEY 启动期 lifespan 抛(import 期不抛)
    L20 create_index 就绪轮询超时/失败态(源码核对)
    L22 tracing _astream list 形式 content 聚积不落 None
(L6/L9-L13 前端与 RAG_program 走逻辑核对 + vite build, 不在本文件测。)
"""
import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

#  环境快照(对齐 test_boundary_medium): api.py 模块级 load_dotenv 会注入
#  lawApp_LangGraph/.env 键, teardown 清掉防污染同进程后续文件
_ENV_KEYS = frozenset(os.environ)


@pytest.fixture(autouse=True)
def _reset_module_globals():
    yield
    import lawApp_LangGraph.FastAPI.api as api
    import lawApp_LangGraph.db as db

    api._SESSION_LOCKS.clear()
    db._pool = None
    for k in set(os.environ) - set(_ENV_KEYS):
        if k != "PYTEST_CURRENT_TEST":
            os.environ.pop(k, None)


def _run(coro):
    return asyncio.run(coro)


# ── L1: 审计旁路日志 + feedback 端点降级 ──


def test_l1_safe_audit_failure_logged(monkeypatch, caplog):
    """_safe_audit 失败不再零日志静默(caplog 捕到 warning)。"""
    import lawApp_LangGraph.FastAPI.api as api
    import lawApp_LangGraph.db as db

    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "upsert_session", boom)
    monkeypatch.setattr(db, "record_audit", boom)
    with caplog.at_level(logging.WARNING, logger="lawApp.api"):
        _run(api._safe_audit("AT-aaaaaaaaaaaa", "citations", {"x": 1}))
    assert any("审计写入失败" in r.getMessage() for r in caplog.records), (
        "审计旁路失败应记 warning"
    )


def test_l1_feedback_write_failure_returns_200_degraded(monkeypatch):
    """反馈写入失败统一契约: 200 + degraded 标记, 不再 500。"""
    import lawApp_LangGraph.db as db
    import lawApp_LangGraph.FastAPI.api as api
    from fastapi.testclient import TestClient

    async def boom(*a, **k):
        raise RuntimeError("db down")

    async def ok(*a, **k):
        return None

    client = TestClient(api.app)  # 不进 lifespan
    monkeypatch.setattr(db, "record_feedback", boom)
    r = client.post(
        "/feedback", json={"session_id": "AT-aaaaaaaaaaaa", "rating": 5}
    )
    assert r.status_code == 200
    body = r.json()
    assert body.get("degraded") is True

    monkeypatch.setattr(db, "record_feedback", ok)
    r2 = client.post(
        "/feedback", json={"session_id": "AT-aaaaaaaaaaaa", "rating": 5}
    )
    assert r2.status_code == 200
    assert r2.json().get("status") == "success"
    assert "degraded" not in r2.json()


# ── L2: sse_event default=str ──


def test_l2_sse_event_serializes_non_json_native_payload():
    """payload 含 datetime/自定义对象不再 TypeError。"""
    from lawApp_LangGraph.FastAPI.utils import sse_event

    class _Opaque:
        def __str__(self):
            return "<opaque>"

    frame = sse_event(
        "interrupt",
        {"at": datetime(2026, 9, 21, 12, 0, 0), "obj": _Opaque()},
    )
    assert frame.startswith("data: ")
    assert "interrupt" in frame and "2026-09-21" in frame and "<opaque>" in frame


# ── L3/L7/L20: 源码级核对(重依赖模块不 import) ──


def test_l3_embed_dim_wired_via_settings():
    """runtime/mcp_server 两处 dims 接 settings.embed_dim(不再硬编码 1024)。"""
    from lawApp_LangGraph.config import settings

    assert settings.embed_dim == 1024  # 旋钮存在, 默认不变
    for rel in (
        "lawApp_LangGraph/runtime.py",
        "lawApp_LangGraph/mcp/mcp_server.py",
    ):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert '"dims": settings.embed_dim' in src, f"{rel} 未接 embed_dim 旋钮"
        assert '"dims": 1024' not in src, f"{rel} 仍残留硬编码维度"


def test_l7_mcp_search_cases_passes_through_top_k():
    """search_cases 透传调用方 top_k 并钳 1..50(不再静默固定 20)。"""
    src = (
        ROOT / "lawApp_LangGraph/mcp/mcp_server.py"
    ).read_text(encoding="utf-8")
    assert "max(1, min(int(top_k), 50))" in src, "top_k 未钳制 1..50"
    assert '"top_k": k' in src, "未透传钳制后的 top_k"
    assert "docs[:k]" in src, "展示层未同步使用钳制后的 k"
    assert '"top_k": 20' not in src, "仍残留静默固定 20"


def test_l20_create_index_loop_has_timeout_and_failed_states():
    """就绪轮询有总超时与失败态直判(不再无限转)。"""
    src = (
        ROOT / "lawApp_LangGraph/RAG_service/RAG_program.py"
    ).read_text(encoding="utf-8")
    assert "TimeoutError" in src
    assert "600" in src
    assert "initialization_failed" in src


# ── L4: search_memory fallback 裸跑 ──


def test_l4_search_memory_fallback_failure_returns_error(monkeypatch):
    """语义检索 TypeError → 降级 asearch 也炸 → status:"error" 不再裸抛。"""
    from lawApp_LangGraph.tools import db_tools

    class _BadStore:
        async def asearch(self, namespace, query=None, limit=None):
            if query is not None:
                raise TypeError("no vector index")
            raise RuntimeError("pg down")  # fallback 也失败

    monkeypatch.setattr("langgraph.config.get_store", lambda: _BadStore())
    res = _run(db_tools.search_memory.ainvoke({"query": "婚姻", "top_k": 3}))
    assert res["status"] == "error"
    assert res["memory_results"] == []


def test_l4_search_memory_fallback_success_still_works(monkeypatch):
    """语义检索 TypeError → 降级 asearch 成功: 原有降级路径不受影响。"""
    from lawApp_LangGraph.tools import db_tools

    class _Item:
        def __init__(self):
            self.item = {"memory_type": "general", "content": "既往结论"}
            self.created_at = None
            self.score = 0.9

    class _NoIndexStore:
        async def asearch(self, namespace, query=None, limit=None):
            if query is not None:
                raise TypeError("no vector index")
            return [_Item()]

    monkeypatch.setattr("langgraph.config.get_store", lambda: _NoIndexStore())
    res = _run(db_tools.search_memory.ainvoke({"query": "婚姻", "top_k": 3}))
    assert res["status"] == "success"
    assert res["count"] == 1


# ── L5: evaluate_case_relevance 假结论 ──


def test_l5_evaluate_case_relevance_rejects_dict_without_docs():
    """dict 输入(取不到 rag_documents/仍非列表)→ 显式 error, 不产假结论。"""
    from lawApp_LangGraph.tools.rag_tools import evaluate_case_relevance

    f = evaluate_case_relevance.func  # 直调底层函数绕过 args 校验
    res = f(documents={"rag_documents": "不是列表"})
    assert res["status"] == "error"
    assert "输入格式错误" in res["message"]

    res2 = f(documents={"unexpected_key": 1})  # 取不到 rag_documents
    assert res2["status"] == "error"


def test_l5_evaluate_case_relevance_unwraps_rag_documents():
    """dict 带 rag_documents 列表 → 解包后正常评估(不再报错/假结论)。"""
    from lawApp_LangGraph.tools.rag_tools import evaluate_case_relevance

    f = evaluate_case_relevance.func
    docs = [
        {
            "id": "d1",
            "year": "2020",
            "case_number": "(2020)京01号",
            "case_cause": "离婚",
            "chunk_text": "案情A",
            "hybrid_score": 0.8,
        },
        {
            "id": "d2",
            "year": "2021",
            "case_number": "(2021)粤03号",
            "case_cause": "抚养",
            "chunk_text": "案情B",
            "hybrid_score": 0.05,
        },
    ]
    res = f(documents={"status": "success", "rag_documents": docs})
    assert res["evaluation"].total == 2
    assert res["evaluation"].correct_count == 1
    assert res["evaluation"].incorrect_count == 1


# ── L8: health_check 轻量化 ──


def test_l8_health_check_lightweight_no_search():
    """health_check 不触发 self.search(模型全冷启动), 返回 ok + 后端名。"""
    from lawApp_LangGraph.RAG_service.base import BaseRetriever

    class _Probe(BaseRetriever):
        def __init__(self):
            self.search_called = False

        async def search(self, query, top_k=20, rerank_top_n=5, alpha=0.4,
                          namespace=None):
            self.search_called = True
            return []

    probe = _Probe()
    result = _run(probe.health_check())
    assert probe.search_called is False, "health_check 不应触发检索/模型加载"
    assert result == {"status": "ok", "backend": "_Probe"}


# ── L15: degrade 询问计数门槛 ──


def test_l15_degrade_threshold_scales_with_ask_count():
    """首次达阈值即询问; 询问过一次后门槛翻倍(degrade_used 不再永久关闭)。"""
    from lawApp_LangGraph.LangGraph_lawApp import (
        route_after_executor,
        route_after_merge,
    )
    from lawApp_LangGraph.config import settings
    from lawApp_LangGraph.state import AgentState

    th = settings.error_streak_threshold
    # 首次: streak == 阈值 → 询问
    s0 = AgentState(query="q", error_streak=th, degrade_ask_count=0)
    assert route_after_executor(s0) == "hitl_degrade"
    assert route_after_merge(s0) == "hitl_degrade"
    # 询问过一次: 同等 streak 不再询问(门槛翻倍), 回常规路径
    s1 = AgentState(query="q", error_streak=th, degrade_ask_count=1)
    assert route_after_executor(s1) == "replan_check"
    assert route_after_merge(s1) == "replan_check"
    # 继续失败达翻倍门槛 → 允许再次询问(旧实现 degrade_used=True 永不询问)
    s2 = AgentState(
        query="q", error_streak=th * 2, degrade_ask_count=1, degrade_used=True
    )
    assert route_after_executor(s2) == "hitl_degrade"
    assert route_after_merge(s2) == "hitl_degrade"
    # 两次询问后: 三倍门槛才再问
    s3 = AgentState(query="q", error_streak=th * 3, degrade_ask_count=2)
    assert route_after_executor(s3) == "hitl_degrade"
    s4 = AgentState(query="q", error_streak=th * 3 - 1, degrade_ask_count=2)
    assert route_after_executor(s4) != "hitl_degrade"


def test_l15_state_defaults_backfill_old_checkpoints():
    """旧 checkpoint 无 degrade_ask_count 字段 → Pydantic 默认 0 兼容。"""
    from lawApp_LangGraph.state import AgentState

    st = AgentState.model_validate({"query": "q"})  # 缺字段按默认重建
    assert st.degrade_ask_count == 0


# ── L19: 缺 DeepSeek key 启动期爆 ──


def _patch_lifespan_env(monkeypatch, has_key):
    import lawApp_LangGraph.runtime as runtime
    from lawApp_LangGraph.config import settings

    async def noop():
        pass

    monkeypatch.setattr(runtime, "setup_runtime", noop)
    monkeypatch.setattr(runtime, "teardown_runtime", noop)
    monkeypatch.setattr(settings, "deepseek_api_key", "sk-test" if has_key else None)
    import lawApp_LangGraph.FastAPI.api as api

    return api


def test_l19_missing_deepseek_key_fails_loud_at_startup(monkeypatch):
    """lifespan 启动期缺 key → RuntimeError(而非首次 LLM 调用才炸)。"""
    api = _patch_lifespan_env(monkeypatch, has_key=False)

    async def enter():
        async with api.lifespan(api.app):
            pass

    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        asyncio.run(enter())


def test_l19_present_deepseek_key_startup_ok(monkeypatch):
    """key 在位 → lifespan 正常进出, 不抛。"""
    api = _patch_lifespan_env(monkeypatch, has_key=True)

    async def enter():
        async with api.lifespan(api.app):
            pass

    asyncio.run(enter())  # 不抛即通过


def test_l19_import_api_module_does_not_raise(monkeypatch):
    """模块 import(不进 lifespan)不受 key 缺失影响 —— 校验只在 startup 路径。"""
    from lawApp_LangGraph.config import settings

    monkeypatch.setattr(settings, "deepseek_api_key", None)
    import importlib

    import lawApp_LangGraph.FastAPI.api as api

    importlib.reload(api)  # key 缺失下重新执行模块级代码, 不应抛


# ── L22: tracing _astream list 形式 content 聚积 ──


def test_l22_astream_accumulates_list_content(monkeypatch):
    """list 形式 content(内容块)也进聚积文本, span output 不再落 None。"""
    import lawApp_LangGraph.tracing as tracing
    from langchain_core.messages import AIMessageChunk, HumanMessage

    async def fake_astream(self, messages, stop=None, run_manager=None, **kwargs):
        yield AIMessageChunk(content=[{"type": "text", "text": "案情分析"}])
        yield AIMessageChunk(content="结论")

    monkeypatch.setattr(
        "langchain_openai.ChatOpenAI._astream", fake_astream
    )
    cls = tracing.get_instrumented_llm_cls()
    llm = cls(model="test-model", api_key="k")
    run = tracing.set_run(
        tracing.RunContext(run_id="t1", session_id="s1", run_type="eval")
    )
    out = [
        c
        for c in _run(
            _collect(llm, [HumanMessage(content="问题")])
        )
    ]
    assert len(out) == 2
    llm_spans = [s for s in run.spans if s.span_type == "llm"]
    assert llm_spans, "llm span 应已落"
    content = llm_spans[-1].output
    assert content is not None, "list 形式 content 不应让聚积文本落 None"
    assert "案情分析" in content and "结论" in content


async def _collect(llm, messages):
    return [c async for c in llm._astream(messages)]
