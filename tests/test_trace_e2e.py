"""P1 集成 — SSE 全链路落 trace(替身 LLM 同 test_smoke 思路; PG 掉线 SKIP)。

事件路由以实际代码为准: GET /attorney/ask/stream?query=...
(计划文档 Step 1 原稿写 POST /ask/stream 为笔误, 该路由是 GET 且已 deprecated)
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

from langchain_core.runnables import Runnable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class _FakeMsg:
    def __init__(self, content=""):
        self.content = content
        self.tool_calls = []


class _FakeVerdict(SimpleNamespace):
    """risk_gate 与 element_assess 共用: 闲聊分类 → chitchat 短路。"""


class _FakeChain(Runnable):
    """`PromptTemplate | llm.with_structured_output(...)` 替身。

    RunnableSequence 只收 Runnable → 必须继承(invoke 为抽象方法)。
    """

    def __init__(self, verdict):
        self._v = verdict

    def invoke(self, _inp, config=None, **kwargs):
        return self._v

    async def ainvoke(self, _inp, config=None, **kwargs):
        return self._v


class _FakeLLM(Runnable):
    """LLM 替身: structured → 固定 verdict; astream → 固定回答(供 chitchat 管道)。"""

    def __init__(self, verdict, text):
        self._v = verdict
        self._text = text

    def with_structured_output(self, schema, **kwargs):
        return _FakeChain(self._v)

    async def astream(self, _prompt, config=None, **kwargs):
        yield _FakeMsg(self._text)

    async def ainvoke(self, msgs, config=None, **kwargs):
        return _FakeMsg(self._text)

    def invoke(self, msgs, config=None, **kwargs):
        return _FakeMsg(self._text)


def _patch_llms(monkeypatch):
    fake = _FakeLLM(
        _FakeVerdict(
            high_risk=False, need_clarification=False, reason="", question="",
            question_category="chitchat", applicable=False, element_updates=(),
            na_keys=(), promote_keys=(), questions=(), done=False,
            plan=(), reasoning=(), insufficient_reason="none", needs_replan=False,
        ),
        "冒烟回答",
    )
    import lawApp_LangGraph.LangGraph_lawApp as app_mod

    monkeypatch.setattr(app_mod, "get_executor_llm", lambda: fake)
    monkeypatch.setattr(app_mod, "get_planner_llm", lambda: fake)


def _pg_ok() -> bool:
    async def _probe():
        # 独立短连接探测 — 不碰 get_pool 全局池
        # (asyncio.run 的循环 A 建池会绑死, TestClient 循环 B 关池时炸)
        from psycopg import AsyncConnection

        from lawApp_LangGraph.db import build_dsn

        async with await AsyncConnection.connect(build_dsn()) as conn:
            await conn.execute("SELECT 1")

    try:
        asyncio.run(_probe())
        return True
    except Exception:
        return False


def test_sse_run_persists_trace(monkeypatch):
    import pytest

    if not _pg_ok():
        pytest.skip("PG 不可用, 显式跳过(不 mock)")
    _patch_llms(monkeypatch)
    from fastapi.testclient import TestClient
    from lawApp_LangGraph.FastAPI.api import app

    with TestClient(app) as client:
        with client.stream(
            "GET", "/attorney/ask/stream", params={"query": "你好"}
        ) as resp:
            assert resp.status_code == 200
            body = "".join(line for line in resp.iter_lines())

    assert "done" in body  # 全流程收尾
    assert "冒烟回答" in body  # chitchat 短路回答

    # 从 DB 侧取最新 run 验证(不依赖事件解析细节)
    import lawApp_LangGraph.db as db

    async def _verify():
        pool = await db.get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT run_id, run_type, status, final_answer, metrics "
                "FROM trace_runs WHERE session_id IS NOT NULL "
                "ORDER BY started_at DESC LIMIT 1"
            )
            row = await cur.fetchone()
            assert row is not None, "trace_runs 必须有行"
            rid, run_type, status, answer, metrics = row
            assert run_type == "live_ask" and status == "ok"
            assert answer == "冒烟回答"
            assert metrics["node_count"] >= 2  # ingest + risk_gate + element_assess + chitchat
            cur = await conn.execute(
                "SELECT span_type, name, latency_ms, state IS NOT NULL "
                "FROM trace_spans WHERE run_id=%s",
                (rid,),
            )
            spans = await cur.fetchall()
            names = {(s[0], s[1]) for s in spans}
            assert ("node", "ingest") in names and ("node", "chitchat") in names
            # values 回填: node span 必有 state
            assert all(s[3] for s in spans if s[0] == "node")
            # 清理
            await conn.execute("DELETE FROM trace_spans WHERE run_id=%s", (rid,))
            await conn.execute("DELETE FROM trace_runs WHERE run_id=%s", (rid,))

    asyncio.run(_verify())
