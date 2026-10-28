"""MonitorWriter / minor_states DDL — 单事务开闭行(真实 PG, 不可用 skip)。"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv("STAGE_ZIPPER", "1")
    monkeypatch.setenv("MINOR_STATES", "1")


def _pg_ok() -> bool:
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


def _skip_no_pg():
    if not _pg_ok():
        pytest.skip("PG 不可用, 显式跳过(不 mock)")


def test_minor_ddl_idempotent():
    _skip_no_pg()

    async def _run():
        from lawApp_LangGraph.db import close_pool, ensure_tables, get_pool

        pool = await get_pool()
        async with pool.connection() as conn:
            await ensure_tables(conn)
            await ensure_tables(conn)
            cur = await conn.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_name = 'minor_states'"
            )
            assert (await cur.fetchone())[0] == 1
            cur = await conn.execute(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name='trace_runs' AND column_name IN "
                "('parent_run_id','current_node')"
            )
            assert (await cur.fetchone())[0] == 2
        await close_pool()

    asyncio.run(_run())


def test_writer_open_close_single_transaction_and_parent_link():
    _skip_no_pg()
    from lawApp_LangGraph import minor_state as ms

    rid = f"test-minor-{time.time_ns()}"
    parent = f"test-minor-parent-{time.time_ns()}"

    async def _run():
        from lawApp_LangGraph.db import close_pool, get_pool, start_trace_run

        await start_trace_run(parent, "sess-x", "live_ask", "attorney", "q")
        await start_trace_run(rid, "sess-x", "live_resume", "attorney", "q", parent)
        ms.enqueue_open(
            parent, "sess-x", "ask_element", 1,
            input_text="问", parent_run_id=None,
        )
        ms.enqueue_close(
            parent, "ask_element", 1, status="interrupted", latency_ms=5,
            phase="waiting_human", output_text="等待",
        )
        await ms.drain(5)
        # 模拟父 run 的 interrupted 行已存在
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE stage_chain SET status='interrupted' WHERE run_id=%s", (parent,)
            )
            await conn.commit()
            assert cur.rowcount == 1

        ms.enqueue_open(
            rid, "sess-x", "ask_element", 1,
            input_text="问", attempt=2, parent_run_id=parent,
        )
        ms.enqueue_close(
            rid, "ask_element", 1, status="ok", latency_ms=7,
            output_text="用户回答: 已婚",
        )
        await ms.drain(5)

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT s.status, s.latency_ms, m.phase, m.attempt, "
                "m.output_text, m.parent_stage_id, r.current_node, r.status "
                "FROM stage_chain s JOIN minor_states m ON m.stage_id=s.id "
                "JOIN trace_runs r ON r.run_id=s.run_id "
                "WHERE s.run_id=%s", (rid,)
            )
            row = await cur.fetchone()
            assert row is not None
            assert row[0] == "ok" and row[1] == 7
            assert row[2] == "done" and row[3] == 2
            assert "已婚" in row[4]
            assert row[5] is not None
            assert row[6] is None and row[7] == "running"
        await close_pool()

    asyncio.run(_run())


def test_minor_disabled_writes_only_stage(monkeypatch):
    _skip_no_pg()
    monkeypatch.setenv("MINOR_STATES", "0")
    from lawApp_LangGraph import minor_state as ms

    rid = f"test-minor-off-{time.time_ns()}"

    async def _run():
        from lawApp_LangGraph.db import close_pool, get_pool, start_trace_run

        await start_trace_run(rid, "sess-x", "live_ask", "attorney", "q")
        ms.enqueue_open(rid, "sess-x", "ingest", 1, input_text="q")
        ms.enqueue_close(rid, "ingest", 1, status="ok", latency_ms=3)
        await ms.drain(5)
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT count(*) FROM stage_chain WHERE run_id=%s", (rid,)
            )
            assert (await cur.fetchone())[0] == 1
            cur = await conn.execute(
                "SELECT count(*) FROM minor_states m JOIN stage_chain s "
                "ON s.id=m.stage_id WHERE s.run_id=%s", (rid,)
            )
            assert (await cur.fetchone())[0] == 0
        await close_pool()

    asyncio.run(_run())


def test_route_backfill():
    _skip_no_pg()
    from lawApp_LangGraph import minor_state as ms

    rid = f"test-minor-route-{time.time_ns()}"

    async def _run():
        from lawApp_LangGraph.db import close_pool, get_pool, start_trace_run

        await start_trace_run(rid, "sess-x", "live_ask", "attorney", "q")
        ms.enqueue_open(rid, "sess-x", "ingest", 1)
        ms.enqueue_close(rid, "ingest", 1, status="ok", latency_ms=1)
        ms.enqueue_route(rid, "ingest", 1, "risk_gate", "固定边")
        await ms.drain(5)
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT route, route_reason FROM minor_states m "
                "JOIN stage_chain s ON s.id=m.stage_id WHERE s.run_id=%s", (rid,)
            )
            assert (await cur.fetchone()) == ("risk_gate", "固定边")
        await close_pool()

    asyncio.run(_run())
