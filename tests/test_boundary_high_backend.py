"""边界审查高危批(H1-H10 后端)回归测试。

不依赖真 PG / DeepSeek / MCP server / Pinecone —— 全部 stub/monkeypatch;
不碰存量测试文件。覆盖:
    H1  _tool_by_name 运行期注册表;executor 缺工具 → 步骤 failed 不抛
    H2  POST /ask/pdf 路由存在且通
    H3  确认词 fast-path(完全相等),其余走 semantic_confirm
    H4  同 session 并发流 409;_REASONING_BUS set fan-out
    H5  resume 空 sid / 无 pending interrupt → 400
    H6  Pinecone 半初始化单例不污染(失败后可重试)
    H7  pgvector NULL 行不崩 + SQL 过滤
    H8  MCP 失败可重试 / get_tools 超时保护 / _loaded 语义
    H9  PDF 文件名路径穿越清洗
    H10 SSE keepalive 注释帧(": ping")
"""
import asyncio
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


#  模块级全局清理(防单例污染串测试)
@pytest.fixture(autouse=True)
def _reset_module_globals(monkeypatch):
    yield
    import lawApp_LangGraph.FastAPI.api as api
    import lawApp_LangGraph.LangGraph_lawApp as app
    import lawApp_LangGraph.RAG_service.pinecone_retriever as pcr
    import lawApp_LangGraph.mcp.mcp_client as mcp_client
    from lawApp_LangGraph.tools import MCP_TOOLS

    api._SESSION_LOCKS.clear()
    app._REASONING_BUS.clear()
    pcr._service = None
    mcp_client._loaded = False
    mcp_client._client = None
    mcp_client._mcp_tools = []
    # 移除测试注入的 fake MCP 工具
    MCP_TOOLS[:] = [t for t in MCP_TOOLS if not str(getattr(t, "name", "")).startswith("fake_")]


def _run(coro):
    return asyncio.run(coro)


# ── H1: TOOL_BY_NAME 导入期快照 → 运行期 _tool_by_name ──


def test_h1_post_registered_tool_found_at_runtime():
    """runtime 期注入的 MCP 工具能被 _tool_by_name 查到(旧快照查不到)。"""
    from lawApp_LangGraph.LangGraph_lawApp import _tool_by_name
    from lawApp_LangGraph.tools import MCP_TOOLS

    fake = types.SimpleNamespace(name="fake_mcp_search", description="d")
    MCP_TOOLS.append(fake)
    assert _tool_by_name("fake_mcp_search") is fake
    assert _tool_by_name("no_such_tool") is None


def test_h1_normalize_plan_keeps_runtime_registered_tool():
    """planner 计划里的后注册工具名不被剥成 None(旧 TOOL_BY_NAME 会剥)。"""
    from lawApp_LangGraph.LangGraph_lawApp import _normalize_plan
    from lawApp_LangGraph.tools import MCP_TOOLS

    fake = types.SimpleNamespace(name="fake_mcp_search", description="d")
    MCP_TOOLS.append(fake)
    schema = types.SimpleNamespace(
        plan=[
            types.SimpleNamespace(step_id=1, description="a", tool_name="fake_mcp_search"),
            types.SimpleNamespace(step_id=2, description="b", tool_name="no_such_tool"),
        ]
    )
    steps = _normalize_plan(schema)
    assert steps[0].tool_name == "fake_mcp_search"
    assert steps[1].tool_name is None


def test_h1_executor_missing_tool_marks_step_failed_not_raise():
    """executor 遇到已消失工具:步骤记 failed + error 字段,不抛 KeyError。"""
    from lawApp_LangGraph.LangGraph_lawApp import executor_node
    from lawApp_LangGraph.state import AgentState, PlanStep

    state = AgentState(
        query="测试问题",
        plan=[PlanStep(step_id=1, description="调用工具", tool_name="ghost_tool")],
        current_step_index=0,
    )
    out = _run(executor_node(state, None))
    assert out["plan"][0].status == "failed"
    assert out["current_step_index"] == 1
    assert "ghost_tool 不可用" in out["error"]
    assert out["error_streak"] == 1


# ── H2: POST /ask/pdf 无装饰器 → 404 ──


def test_h2_ask_pdf_route_exists_and_streams_file(tmp_path, monkeypatch):
    """/ask/pdf 路由存在:mock graph + PDF 工具,端到端返回 200 与文件字节。"""
    from fastapi.testclient import TestClient

    import lawApp_LangGraph.FastAPI.api as api
    import lawApp_LangGraph.tools.tools as tools_mod

    pdf_file = tmp_path / "out.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 fake")

    class _FakeGraph:
        async def ainvoke(self, inp, config=None):
            return {"query": "测试", "final_answer": "# 报告\n内容"}

    async def _fake_ainvoke(args):
        return {"pdf_path": str(pdf_file), "is_pdf_output": True}

    monkeypatch.setattr(api, "get_graph", lambda: _FakeGraph())
    monkeypatch.setattr(tools_mod, "markdown_to_pdf", types.SimpleNamespace(ainvoke=_fake_ainvoke))

    client = TestClient(api.app)  # 不进 lifespan: 不触 PG/MCP 装配
    r = client.post("/ask/pdf", json={"query": "测试问题", "session_id": "T-PDF-1"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/pdf")
    assert r.content == b"%PDF-1.4 fake"


# ── H3: 确认词子串误判 → fast-path + semantic_confirm ──


def _patch_semantic_confirm(monkeypatch, proceed, calls):
    """stub LangGraph_lawApp.semantic_confirm(utils 在调用期导入)。"""
    import lawApp_LangGraph.LangGraph_lawApp as app

    async def fake(request_text, answer):
        calls.append(answer)
        return proceed

    monkeypatch.setattr(app, "semantic_confirm", fake)


def test_h3_budget_free_text_not_misjudged_as_finish(monkeypatch):
    """「婚姻关系已于2020年结束」是补充,不判 finish;语义判定返回同意继续。"""
    from lawApp_LangGraph.FastAPI.utils import normalize_resume

    calls: list[str] = []
    _patch_semantic_confirm(monkeypatch, True, calls)

    out = _run(normalize_resume("budget_confirm", "婚姻关系已于2020年结束", "是否补充信息?"))
    assert out == "婚姻关系已于2020年结束"  # 原文透传,不是 "finish"
    assert calls, "自由文本必须走 semantic_confirm(LLM 意图判定)"


def test_h3_budget_pure_command_word_hits_fast_path(monkeypatch):
    """纯「收尾」→ fast-path 直接 finish,不调 LLM。"""
    from lawApp_LangGraph.FastAPI.utils import normalize_resume

    calls: list[str] = []
    _patch_semantic_confirm(monkeypatch, True, calls)
    assert _run(normalize_resume("budget_confirm", "收尾", "m")) == "finish"
    assert _run(normalize_resume("budget_confirm", "FINISH ", "m")) == "finish"
    assert _run(normalize_resume("budget_confirm", "", "m")) == "finish"
    assert not calls, "纯指令词不允许触发 LLM"


def test_h3_degrade_command_words_and_free_text(monkeypatch):
    """纯「重试」→ retry;「不要再重试了,终止吧」走 LLM → abort(旧子串匹配会误判 retry)。"""
    from lawApp_LangGraph.FastAPI.utils import normalize_resume

    calls: list[str] = []
    _patch_semantic_confirm(monkeypatch, False, calls)  # LLM 判:不同意继续

    assert _run(normalize_resume("degrade_confirm", "重试", "m")) == "retry"
    assert _run(normalize_resume("degrade_confirm", "跳过", "m")) == "skip"
    assert _run(normalize_resume("degrade_confirm", "终止", "m")) == "abort"
    assert not calls

    # 自由文本(含"重试"字样但语义为终止)必须交 LLM,而不是子串命中 retry
    out = _run(normalize_resume("degrade_confirm", "不要再重试了,终止吧", "m"))
    assert out == "abort"
    assert calls


def test_h3_is_command_word_exact_match_only():
    from lawApp_LangGraph.FastAPI.utils import is_command_word

    assert is_command_word("收尾") == "finish"
    assert is_command_word(" Continue ") == "continue"
    assert is_command_word("婚姻关系已于2020年结束") is None
    assert is_command_word("") is None


def test_h3_budget_node_uses_fast_path():
    """节点侧 hitl_budget_node 复用 is_command_word:补充文本不判收尾。"""
    from langgraph.types import interrupt

    import lawApp_LangGraph.LangGraph_lawApp as app

    captured = {}

    def fake_interrupt(payload):
        captured["payload"] = payload
        return captured.setdefault("resume", "婚姻关系已于2020年结束")

    # 直接替换模块级 interrupt(仅本用例内,用完后恢复)
    orig = app.interrupt
    app.interrupt = fake_interrupt
    try:
        state = types.SimpleNamespace(
            evaluation=None, rag_documents=[], error=None, query="q",
            budget_hitl_used=False,
        )
        out = app.hitl_budget_node(state)
        assert out["hitl_event"]["choice"] == "supplement"
        assert "[用户补充信息] 婚姻关系已于2020年结束" in out["query"]

        captured["resume"] = "收尾"
        out = app.hitl_budget_node(state)
        assert out["hitl_event"]["choice"] == "finish"
    finally:
        app.interrupt = orig


# ── H4: 同 session 并发流 + _REASONING_BUS fan-out ──


def test_h4_second_stream_same_session_gets_409(monkeypatch):
    """同 sid 第二个流请求 → 409 session_busy。"""
    from fastapi.testclient import TestClient

    import lawApp_LangGraph.FastAPI.api as api

    async def _no_upsert(sid):
        return None

    monkeypatch.setattr(api, "_safe_upsert_session", _no_upsert)

    sid = "T-409-1"

    async def _hold():
        lock = await api._acquire_session_lock(sid)
        return lock

    _run(_hold())  # 模拟第一条流持有锁

    client = TestClient(api.app)
    r = client.get("/attorney/ask/stream", params={"query": "测试", "session_id": sid})
    assert r.status_code == 409
    assert "session_busy" in r.json()["detail"]


def test_h4_reasoning_bus_fanout_multi_subscriber():
    """总线 set fan-out:两个订阅都收到;断开只摘自己的队列。"""
    import lawApp_LangGraph.LangGraph_lawApp as app

    q1 = app.open_reasoning_channel("T-BUS-1")
    q2 = app.open_reasoning_channel("T-BUS-1")
    assert len(app._REASONING_BUS["T-BUS-1"]) == 2

    app._bus_put("T-BUS-1", {"source": "status", "delta": "x"})
    assert q1.get_nowait() == {"source": "status", "delta": "x"}
    assert q2.get_nowait() == {"source": "status", "delta": "x"}

    app.close_reasoning_channel("T-BUS-1", q1)
    assert "T-BUS-1" in app._REASONING_BUS  # q2 仍在
    app._bus_put("T-BUS-1", {"source": "status", "delta": "y"})
    assert q2.get_nowait() == {"source": "status", "delta": "y"}

    app.close_reasoning_channel("T-BUS-1", q2)
    assert "T-BUS-1" not in app._REASONING_BUS


# ── H5: resume 无守卫 → 空 sid / 无 pending interrupt 400 ──


class _Snap:
    def __init__(self, interrupt_req):
        intr = (
            types.SimpleNamespace(value=interrupt_req) if interrupt_req else None
        )
        self.interrupts = [intr] if intr else []
        self.values = {}


class _FakeResumeGraph:
    """aget_state: pending 标志控制;ainvoke: 恢复并清 pending。"""

    def __init__(self):
        self.pending = types.SimpleNamespace(
            value={"type": "risk_confirm", "message": "确认继续咨询吗?"}
        )
        self.ainvoke_calls = 0

    async def aget_state(self, config):
        return _Snap(dict(self.pending.value) if self.pending.value else None)

    async def ainvoke(self, cmd, config=None):
        self.ainvoke_calls += 1
        self.pending.value = None  # 恢复后无 pending interrupt
        return {"query": "q", "final_answer": "ok", "reasoning": []}


def test_h5_resume_empty_session_id_400(monkeypatch):
    import lawApp_LangGraph.FastAPI.api as api
    from fastapi.testclient import TestClient

    client = TestClient(api.app)
    r = client.post("/ask/resume", json={"session_id": "   ", "answer": "确认"})
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_session_id"

    r = client.post(
        "/ask/resume/stream", json={"session_id": "   ", "answer": "确认"}
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_session_id"


def test_h5_double_resume_second_gets_400(monkeypatch):
    """第一次 resume 正常;pending 消费后第二次 → 400 no_pending_interrupt。"""
    import lawApp_LangGraph.FastAPI.api as api
    from fastapi.testclient import TestClient

    fake = _FakeResumeGraph()

    async def _no_audit(*a, **k):
        return None

    monkeypatch.setattr(api, "get_graph", lambda: fake)
    monkeypatch.setattr(api, "_safe_audit", _no_audit)

    client = TestClient(api.app)
    # 第一次: interrupt 在 → 恢复成功
    r1 = client.post(
        "/ask/resume", json={"session_id": "T-RES-1", "answer": "确认"}
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["final_answer"] == "ok"
    assert fake.ainvoke_calls == 1
    # 第二次: 已无 pending interrupt → 400
    r2 = client.post(
        "/ask/resume", json={"session_id": "T-RES-1", "answer": "确认"}
    )
    assert r2.status_code == 400
    assert r2.json()["detail"] == "no_pending_interrupt"


def test_h5_resume_stream_no_pending_400(monkeypatch):
    import lawApp_LangGraph.FastAPI.api as api
    from fastapi.testclient import TestClient

    fake = _FakeResumeGraph()
    fake.pending.value = None
    monkeypatch.setattr(api, "get_graph", lambda: fake)

    client = TestClient(api.app)
    r = client.post(
        "/ask/resume/stream", json={"session_id": "T-RES-2", "answer": "确认"}
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "no_pending_interrupt"


# ── H6: Pinecone 半初始化单例 ──


def test_h6_failed_init_does_not_pollute_singleton(monkeypatch):
    """index 附着抛错 → _service 保持 None;修复后再次调用可重试成功。"""
    import lawApp_LangGraph.RAG_service.pinecone_retriever as pcr

    class _BadIndexPC:
        def Index(self, name):
            raise RuntimeError("索引附着失败(模拟)")

    class _FailingSvc:
        def __init__(self, **kw):
            self.index_name = kw.get("index_name")
            self.pc = _BadIndexPC()

    fake_mod = types.SimpleNamespace(RAG_service=_FailingSvc)
    monkeypatch.setitem(
        sys.modules, "lawApp_LangGraph.RAG_service.RAG_program", fake_mod
    )
    with pytest.raises(RuntimeError):
        pcr._get_service()
    assert pcr._service is None, "半初始化失败不允许污染全局单例"

    # server 恢复 → 下次调用重试成功(旧实现这里会 RuntimeError('索引未初始化'))
    class _GoodPC:
        def Index(self, name):
            return object()

    class _GoodSvc(_FailingSvc):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.pc = _GoodPC()

    fake_mod.RAG_service = _GoodSvc
    svc = pcr._get_service()
    assert svc is not None
    assert pcr._service is svc


# ── H7: pgvector NULL embedding ──


def test_h7_null_rows_skipped_and_sql_filters(tmp_path, monkeypatch):
    import lawApp_LangGraph.RAG_service.pgvector_retriever as pvr

    captured_sql: list[str] = []

    class _Cur:
        def __init__(self, rows):
            self._rows = rows

        async def fetchall(self):
            return self._rows

    class _Conn:
        def __init__(self, rows):
            self._rows = rows

        async def execute(self, sql, params=None):
            captured_sql.append(sql)
            return _Cur(self._rows)

    class _CM:
        def __init__(self, conn):
            self._conn = conn

        async def __aenter__(self):
            return self._conn

        async def __aexit__(self, *a):
            return False

    class _Pool:
        def __init__(self, conn):
            self._conn = conn

        def connection(self):
            return _CM(self._conn)

    # 行含 NULL hybrid_score(旧代码 float(None) TypeError 崩整次检索)
    rows = [
        (1, "2020", "(2020)京01号", "离婚", "正文A", 0.87),
        (2, "2021", "(2021)沪02号", "继承", "正文B", None),
        (3, "2022", "(2022)粤03号", "抚养", "正文C", 0.52),
    ]

    async def _fake_embed_query(q):
        return [0.1] * 8

    async def _fake_rerank(q, docs, **k):
        return None  # 禁用重排,退回召回排序

    async def _fake_get_pool():
        return _Pool(_Conn(rows))

    import lawApp_LangGraph.db as db

    monkeypatch.setattr(pvr, "embed_query", _fake_embed_query)
    monkeypatch.setattr(pvr, "rerank", _fake_rerank)
    monkeypatch.setattr(db, "get_pool", _fake_get_pool)

    results = _run(pvr.PgvectorRetriever().search("测试", top_k=5, rerank_top_n=5))
    assert len(results) == 2, "NULL 行必须被跳过而不是崩溃"
    assert all(r["hybrid_score"] is not None for r in results)
    assert "embedding IS NOT NULL" in captured_sql[0], "SQL 必须带 NULL 过滤"


# ── H8: MCP client ──


def _patch_mcp_client(monkeypatch, fail_first=0, get_tools_impl=None):
    """替换 langchain_mcp_adapters.client.MultiServerMCPClient + 打开开关。"""
    import lawApp_LangGraph.config as cfg
    import lawApp_LangGraph.mcp.mcp_client as m

    state = {"constructions": 0, "get_tools_calls": 0}

    class _FakeClient:
        def __init__(self, config):
            state["constructions"] += 1

        async def get_tools(self):
            state["get_tools_calls"] += 1
            if get_tools_impl is not None:
                return await get_tools_impl()
            if state["get_tools_calls"] <= fail_first:
                raise ConnectionError("MCP server 不在线(模拟)")
            return [types.SimpleNamespace(name="fake_mcp_tool")]

    import langchain_mcp_adapters.client as adapters_client

    monkeypatch.setattr(adapters_client, "MultiServerMCPClient", _FakeClient)
    monkeypatch.setattr(cfg.settings, "mcp_tools_enabled", "1")
    monkeypatch.setattr(m, "_MCP_GET_TOOLS_TIMEOUT", 10.0)
    return m, state


def test_h8_failed_load_is_retryable(monkeypatch):
    """失败不缓存:下次 get_mcp_tools 重试;成功后 _loaded=True 且不再连接。"""
    m, state = _patch_mcp_client(monkeypatch, fail_first=1)

    first = _run(m.get_mcp_tools())
    assert first == []
    assert m._loaded is False, "失败必须保持可重试状态"

    second = _run(m.get_mcp_tools())
    assert [t.name for t in second] == ["fake_mcp_tool"]
    assert m._loaded is True

    third = _run(m.get_mcp_tools())
    assert third == second
    assert state["get_tools_calls"] == 2, "成功后走缓存, 不再连接 server"


def test_h8_get_tools_timeout_protects_startup(monkeypatch):
    """server 挂 TCP → get_tools 无限等;wait_for 快速失败返回空列表。"""
    import lawApp_LangGraph.mcp.mcp_client as m

    async def _hang():
        await asyncio.sleep(30)
        return []

    _, state = _patch_mcp_client(monkeypatch, get_tools_impl=_hang)
    monkeypatch.setattr(m, "_MCP_GET_TOOLS_TIMEOUT", 0.05)

    import time as _t

    t0 = _t.monotonic()
    out = _run(m.get_mcp_tools())
    elapsed = _t.monotonic() - t0

    assert out == []
    assert m._loaded is False
    assert elapsed < 5, "不能拖死启动(10s 超时机制生效)"


# ── H9: PDF 文件名路径穿越 ──


def _patch_render_pdf(monkeypatch, tmp_path):
    import lawApp_LangGraph.tools.tools as tools_mod

    def _fake_render(styled_html, file_path):
        Path(file_path).write_text("fake-pdf", encoding="utf-8")

    monkeypatch.setenv("PDF_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(tools_mod, "_render_pdf", _fake_render)


@pytest.mark.parametrize(
    "raw_name",
    ["../../evil.pdf", "..\\..\\evil.pdf", "E:\\x\\evil.pdf"],
)
def test_h9_traversal_filename_lands_inside_output_dir(
    tmp_path, monkeypatch, raw_name
):
    """穿越文件名最终落点必须在 PDF_OUTPUT_DIR 内。"""
    from lawApp_LangGraph.tools.tools import markdown_to_pdf

    _patch_render_pdf(monkeypatch, tmp_path)
    out = _run(
        markdown_to_pdf.ainvoke(
            {"markdown_text": "# 标题\n正文", "filename": raw_name}
        )
    )
    assert out["status"] == "success", out
    p = Path(out["pdf_path"]).resolve()
    assert p.parent == tmp_path.resolve(), "落点必须在输出目录内"
    assert p.name == "evil.pdf"
    assert p.exists()


def test_h9_default_and_illegal_filenames(tmp_path, monkeypatch):
    from lawApp_LangGraph.tools.tools import markdown_to_pdf

    _patch_render_pdf(monkeypatch, tmp_path)
    # 缺省文件名 → 时间戳报告名, 落在输出目录
    out = _run(markdown_to_pdf.ainvoke({"markdown_text": "# x"}))
    assert out["status"] == "success"
    assert Path(out["pdf_path"]).resolve().parent == tmp_path.resolve()

    # 点开头(隐藏文件/目录穿越变体) → 显式 error, 不落盘
    out = _run(
        markdown_to_pdf.ainvoke({"markdown_text": "# x", "filename": "...",})
    )
    assert out["status"] == "error"
    assert out["pdf_path"] is None
    assert "文件名非法" in out["message"]


def test_h9_non_string_input_returns_error_not_typeerror(tmp_path, monkeypatch):
    """非字符串 markdown_text → 结构化 error(旧代码 len() 裸 TypeError)。"""
    import lawApp_LangGraph.tools.tools as tools_mod

    _patch_render_pdf(monkeypatch, tmp_path)
    raw = getattr(tools_mod.markdown_to_pdf, "coroutine", None) or getattr(
        tools_mod.markdown_to_pdf, "func"
    )
    out = _run(raw(markdown_text=object(), filename="ok.pdf"))
    assert out["status"] == "error"
    assert out["pdf_path"] is None


# ── H10: SSE keepalive 注释帧 ──


def test_h10_sse_emits_ping_comment_frames(monkeypatch, tmp_path):
    """流未结束期间周期性下发 ': ping' 注释帧;done 正常;会话锁释放。"""
    import lawApp_LangGraph.FastAPI.api as api

    class _SlowGraph:
        async def astream(self, inp, config=None, stream_mode=None):
            await asyncio.sleep(0.3)  # 模拟长流, 触发多个 ping 周期
            return
            yield  # pragma: no cover — 使其成为 async generator

        async def aget_state(self, config):
            return _Snap(None)

    async def _no_flush(run):
        return None

    async def _no_audit(*a, **k):
        return None

    monkeypatch.setattr(api, "get_graph", lambda: _SlowGraph())
    monkeypatch.setattr(api, "flush_run", _no_flush)
    monkeypatch.setattr(api, "_safe_audit", _no_audit)
    monkeypatch.setattr(api, "_SSE_PING_SECONDS", 0.05)

    sid = "T-PING-1"
    lock = asyncio.Lock()

    async def _drive():
        await lock.acquire()
        resp = api._run_sse(sid, {"query": "q"}, mode="attorney", session_lock=lock)
        chunks = []
        async for chunk in resp.body_iterator:
            chunks.append(chunk)
        return "".join(chunks)

    text = _run(_drive())
    assert text.count(": ping\n\n") >= 2, "长流期间必须有 keepalive 注释帧"
    assert '"event": "done"' in text
    assert '"event": "session_id"' in text
    assert not lock.locked(), "流结束后会话锁必须释放"
