"""P1 — trace 表 DDL 幂等 + insert 助手(真实 PG, 不可用显式 SKIP, 不 mock)。"""
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# psycopg async 在 Windows 需 SelectorEventLoop(与 uvicorn loop 工厂同款)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _pg_ok() -> bool:
    """独立连接探测 — 不碰全局连接池, 避免留下绑定已关闭 loop 的池污染后续用例。"""

    async def _probe():
        from psycopg import AsyncConnection

        from lawApp_LangGraph.db import build_dsn

        conn = await AsyncConnection.connect(build_dsn(), autocommit=True)
        try:
            await conn.execute("SELECT 1")
        finally:
            await conn.close()

    try:
        asyncio.run(_probe())
        return True
    except Exception:
        return False


def test_ensure_tables_idempotent_with_trace_ddl():
    if not _pg_ok():
        import pytest

        pytest.skip("PG 不可用, 显式跳过(不 mock)")
    from lawApp_LangGraph.db import get_pool

    async def _run():
        from lawApp_LangGraph.db import ensure_tables

        pool = await get_pool()
        async with pool.connection() as conn:
            await ensure_tables(conn)  # 第一遍建表
            await ensure_tables(conn)  # 第二遍幂等
            cur = await conn.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_name IN ('trace_runs','trace_spans')"
            )
            (n,) = await cur.fetchone()
            assert n == 2
        # 池在本用例自己的 loop 上创建, 结束前原位关闭(跨 loop close 必挂)
        from lawApp_LangGraph.db import close_pool

        await close_pool()

    asyncio.run(_run())


def test_insert_trace_run_and_spans_roundtrip():
    if not _pg_ok():
        import pytest

        pytest.skip("PG 不可用, 显式跳过(不 mock)")
    from lawApp_LangGraph.db import get_pool, insert_trace_run, insert_trace_spans

    rid = f"test-run:{time.time()}"
    now = time.time()

    async def _run():
        await insert_trace_run(
            run_id=rid, session_id="s-test", run_type="live_ask",
            mode="attorney", status="ok", query="测试问题",
            final_answer="测试回答", metrics={"node_count": 1},
            started_at=now, ended_at=now + 1.5,
        )
        await insert_trace_spans([{
            "run_id": rid, "span_type": "node", "name": "ingest",
            "status": "ok", "input": {"query": "测试问题"},
            "output": {"ok": True}, "state": {"query": "测试问题"},
            "latency_ms": 12, "token_usage": None, "started_at": now,
        }])
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT status, metrics FROM trace_runs WHERE run_id=%s", (rid,)
            )
            status, metrics = await cur.fetchone()
            assert status == "ok" and metrics["node_count"] == 1
            cur = await conn.execute(
                "SELECT span_type, name, latency_ms, state FROM trace_spans WHERE run_id=%s",
                (rid,),
            )
            span_type, name, latency_ms, state = await cur.fetchone()
            assert (span_type, name, latency_ms, state["query"]) == (
                "node", "ingest", 12, "测试问题"
            )
            # 清理测试数据
            await conn.execute("DELETE FROM trace_spans WHERE run_id=%s", (rid,))
            await conn.execute("DELETE FROM trace_runs WHERE run_id=%s", (rid,))
        from lawApp_LangGraph.db import close_pool

        await close_pool()

    asyncio.run(_run())


def test_json_default_unpacks_pydantic_and_falls_back_to_str():
    """_json_default(纯 Python, 无 PG): pydantic 模型 → model_dump dict
    (嵌套 list/dict 内同样解包), 非法对象 str 兜底 — 监控页 JsonTree
    分层渲染的前提(字符串化会把整条变 repr)。"""
    import json

    from pydantic import BaseModel

    from lawApp_LangGraph.db import _json_default

    class Inner(BaseModel):
        law_title: str = "民法典"
        content: str = "全文"

    class Outer(BaseModel):
        law_results: list[Inner] = [Inner()]
        status: str = "success"

    # pydantic → dict(嵌套递归解包)
    assert _json_default(Inner()) == {"law_title": "民法典", "content": "全文"}
    dumped = json.loads(json.dumps(Outer(), default=_json_default, ensure_ascii=False))
    assert dumped["law_results"][0]["content"] == "全文"
    assert isinstance(dumped["law_results"][0], dict)  # 非字符串包裹

    # 非法对象 → str 兜底, 观测旁路不抛
    class Weird:
        def __repr__(self):
            return "<weird>"

    assert _json_default(Weird()) == "<weird>"
    assert _json_default(object())  # 任意对象都有兜底值


def test_insert_trace_handles_non_json_native_values():
    """span 内容可能带非 JSON 原生类型 → _json_default 兜住(规格: 全文不截断)。"""
    if not _pg_ok():
        import pytest

        pytest.skip("PG 不可用, 显式跳过(不 mock)")
    from types import SimpleNamespace

    from lawApp_LangGraph.db import get_pool, insert_trace_spans

    rid = f"test-run-obj:{time.time()}"

    async def _run():
        await insert_trace_spans([{
            "run_id": rid, "span_type": "node", "name": "planner",
            "status": "ok",
            "input": {"state": SimpleNamespace(query="对象引用, 非 JSON 原生")},
            "output": None, "state": None,
            "latency_ms": 1, "token_usage": None, "started_at": time.time(),
        }])
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT input::text FROM trace_spans WHERE run_id=%s", (rid,)
            )
            (raw,) = await cur.fetchone()
            assert "对象引用" in raw
            await conn.execute("DELETE FROM trace_spans WHERE run_id=%s", (rid,))
        from lawApp_LangGraph.db import close_pool

        await close_pool()

    asyncio.run(_run())


def test_insert_trace_unpacks_pydantic_to_structured_jsonb():
    """pydantic 模型经 _json_default 解包落 JSONB — 落库后是结构化
    dict(监控页 JsonTree 分层渲染前提), 非整条 repr 字符串。"""
    if not _pg_ok():
        import pytest

        pytest.skip("PG 不可用, 显式跳过(不 mock)")
    from lawApp_LangGraph.db import get_pool, insert_trace_spans
    from lawApp_LangGraph.state import LawsResult

    rid = f"test-run-unpack:{time.time()}"
    law = LawsResult(
        law_title="中华人民共和国民法典", chapter="婚姻家庭编",
        article_number="1079",
        content="夫妻一方要求离婚的, 可以由有关组织进行调解或者直接向人民法院提起离婚诉讼。" * 30,
    )

    async def _run():
        await insert_trace_spans([{
            "run_id": rid, "span_type": "tool", "name": "fetch_laws",
            "status": "ok", "input": "离婚",
            "output": {"law_results": [law], "status": "success", "count": 1},
            "state": None,
            "latency_ms": 1, "token_usage": None, "started_at": time.time(),
        }])
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT output FROM trace_spans WHERE run_id=%s", (rid,)
            )
            (output,) = await cur.fetchone()
            item = output["law_results"][0]
            assert isinstance(item, dict), "pydantic 仍被字符串化(整条 repr)"
            assert item["law_title"] == "中华人民共和国民法典"
            assert len(item["content"]) == len(law.content)  # 全文无截断
            await conn.execute("DELETE FROM trace_spans WHERE run_id=%s", (rid,))
        from lawApp_LangGraph.db import close_pool

        await close_pool()

    asyncio.run(_run())
