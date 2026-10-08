"""阶段拉链表 stage_chain + eval_runs — DDL 幂等 + 开闭行拉链语义(真实 PG,
不可用显式 SKIP 不 mock, 对齐 test_trace_db 模式)。"""
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


def _skip_no_pg():
    if not _pg_ok():
        import pytest

        pytest.skip("PG 不可用, 显式跳过(不 mock)")


def test_ensure_tables_idempotent_with_stage_ddl():
    _skip_no_pg()

    async def _run():
        from lawApp_LangGraph.db import close_pool, ensure_tables, get_pool

        pool = await get_pool()
        async with pool.connection() as conn:
            await ensure_tables(conn)
            await ensure_tables(conn)
            cur = await conn.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_name IN ('stage_chain', 'eval_runs')"
            )
            (n,) = await cur.fetchone()
            assert n == 2
        await close_pool()

    asyncio.run(_run())


def test_zipper_open_then_close():
    """开行→running 开口; 闭行→ended_at 补齐 + is_current 翻 FALSE(拉链语义)。"""
    _skip_no_pg()
    rid = f"test-stage-{time.time_ns()}"

    async def _run():
        from lawApp_LangGraph import db

        await db.open_stage(rid, "sess-z", "planner", 1)
        pool = await db.get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT status, ended_at, is_current FROM stage_chain "
                "WHERE run_id=%s AND node_name='planner' AND seq=1",
                (rid,),
            )
            row = await cur.fetchone()
            assert row == ("running", None, True)
            await db.close_stage(rid, "planner", 1, "ok", 123,
                                 {"rag_top_score": 0.7})
            cur = await conn.execute(
                "SELECT status, ended_at, is_current, latency_ms, "
                "detail->>'rag_top_score' FROM stage_chain "
                "WHERE run_id=%s AND node_name='planner' AND seq=1",
                (rid,),
            )
            row = await cur.fetchone()
            assert row[0] == "ok" and row[1] is not None and row[2] is False
            assert row[3] == 123 and row[4] == "0.7"
            await conn.execute("DELETE FROM stage_chain WHERE run_id=%s", (rid,))
            await conn.commit()
        await db.close_pool()

    asyncio.run(_run())


def test_seq_reloop_distinct_rows():
    """同节点 seq 递增 = 回环重跑各占一行(拉链历史)。"""
    _skip_no_pg()
    rid = f"test-stage-{time.time_ns()}"

    async def _run():
        from lawApp_LangGraph import db

        for seq in (1, 2):
            await db.open_stage(rid, "sess-z", "executor", seq)
            await db.close_stage(rid, "executor", seq, "ok", 10 * seq, None)
        pool = await db.get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT COUNT(*) FROM stage_chain "
                "WHERE run_id=%s AND node_name='executor'",
                (rid,),
            )
            assert (await cur.fetchone())[0] == 2
            await conn.execute("DELETE FROM stage_chain WHERE run_id=%s", (rid,))
            await conn.commit()
        await db.close_pool()

    asyncio.run(_run())


def test_close_stage_missing_row_silent():
    """闭行 UPDATE 无匹配行(开行极端乱序/失败) → 静默忽略不报错(旁路)。"""
    _skip_no_pg()

    async def _run():
        from lawApp_LangGraph import db

        # 不开行直接闭行 — 不得抛异常
        await db.close_stage("no-such-run", "planner", 1, "ok", 5, None)
        await db.close_pool()

    asyncio.run(_run())


def test_eval_run_insert():
    _skip_no_pg()
    lbl = f"lbl-{time.time_ns()}"

    async def _run():
        from lawApp_LangGraph import db

        await db.insert_eval_run("retrieval", lbl, {"hit_rate_at_5": 1.0},
                                  [{"case_id": "x"}])
        pool = await db.get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT dataset, metrics->>'hit_rate_at_5' FROM eval_runs "
                "WHERE label=%s", (lbl,))
            row = await cur.fetchone()
            assert row == ("retrieval", "1.0")
            await conn.execute("DELETE FROM eval_runs WHERE label=%s", (lbl,))
            await conn.commit()
        await db.close_pool()

    asyncio.run(_run())
