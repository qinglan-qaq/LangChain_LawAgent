"""/monitor 端点 — TestClient 直调(无 /api 前缀, 对齐 test_dialogue_events)。

真实 PG: 造 run + 拉链行 + span, 断言 4 端点契约; PG 不可用 SKIP。
"""
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

_T_PREFIX = "api-mon-test"


def _pg_ok() -> bool:
    """独立连接探测 — 不碰全局连接池(对齐 test_dialogue_events)。"""

    async def _probe():
        from lawApp_LangGraph.db import build_dsn
        from psycopg import AsyncConnection

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


def _cleanup() -> None:
    """清掉本套件造的 run/拉链/span 行(独立短连接)。"""

    async def _run():
        from lawApp_LangGraph.db import build_dsn
        from psycopg import AsyncConnection

        conn = await AsyncConnection.connect(build_dsn(), autocommit=True)
        try:
            for table in ("trace_spans", "stage_chain", "trace_runs"):
                await conn.execute(
                    f"DELETE FROM {table} WHERE run_id LIKE %s",
                    (_T_PREFIX + "%",),
                )
        finally:
            await conn.close()

    try:
        asyncio.run(_run())
    except Exception:
        pass


def _insert_fixture() -> str:
    """造一条 run + 拉链行 + span(直连 db); PG 不可用则 skip。"""
    if not _pg_ok():
        pytest.skip("PG 不可用, 显式跳过(不 mock)")

    from lawApp_LangGraph import db

    rid = f"{_T_PREFIX}-{time.time_ns()}"

    async def seed():
        await db.insert_trace_run(
            run_id=rid, session_id="sess-api", run_type="live_ask",
            mode="attorney", status="ok", query="q", final_answer="a",
            metrics={"composite_score": 88, "limit_hit": False,
                     "node_count": 3, "tool_count": 1, "llm_count": 2,
                     "total_latency_ms": 100, "token_prompt": 10,
                     "token_completion": 5, "clarify_rounds": 0,
                     "replan_rounds": 0, "tool_error_count": 0},
            started_at=time.time() - 1, ended_at=time.time(),
        )
        await db.open_stage(rid, "sess-api", "planner", 1)
        await db.close_stage(rid, "planner", 1, "ok", 50, None)
        await db.insert_trace_spans([{
            "run_id": rid, "span_type": "node", "name": "planner",
            "status": "ok", "input": {"q": 1}, "output": {"plan": []},
            "state": None, "latency_ms": 50, "token_usage": None,
            "started_at": time.time() - 0.5,
        }])
        await db.close_pool()

    asyncio.run(seed())
    return rid


def _client():
    from fastapi.testclient import TestClient

    from lawApp_LangGraph.FastAPI.api import app

    return TestClient(app)  # 不进 lifespan, 监控端点不依赖 runtime


#  1. runs 列表(拉链聚合) + run 详情(stages+spans)


def test_monitor_runs_list_and_detail():
    rid = _insert_fixture()
    try:
        with _client() as c:
            r = c.get("/monitor/runs?limit=200")
            assert r.status_code == 200
            item = next((x for x in r.json() if x["run_id"] == rid), None)
            assert item is not None
            assert item["stage_total"] == 1 and item["stage_ok"] == 1
            assert item["stage_running"] == 0
            assert item["metrics"]["composite_score"] == 88

            r2 = c.get(f"/monitor/runs/{rid}/stages")
            assert r2.status_code == 200
            detail = r2.json()
            assert detail["run_id"] == rid
            assert detail["stages"][0]["node_name"] == "planner"
            assert detail["stages"][0]["status"] == "ok"
            assert detail["spans"][0]["name"] == "planner"
    finally:
        _cleanup()


def test_monitor_runs_status_filter():
    rid = _insert_fixture()
    try:
        with _client() as c:
            r = c.get("/monitor/runs?status=error&limit=200")
            assert r.status_code == 200
            assert all(x["status"] == "error" for x in r.json())
            assert not any(x["run_id"] == rid for x in r.json())
    finally:
        _cleanup()


#  2. 总览 + 评测列表


def test_monitor_overview_and_evals():
    rid = _insert_fixture()
    try:
        with _client() as c:
            r = c.get("/monitor/overview")
            assert r.status_code == 200
            body = r.json()
            assert "runs_by_status" in body and "running_stages" in body
            assert "limit_hit_runs" in body and "node_fail_top" in body
            assert "score_distribution" in body
            assert body["runs_by_status"].get("ok", 0) >= 1

            r2 = c.get("/monitor/evals")
            assert r2.status_code == 200
            assert isinstance(r2.json(), list)
    finally:
        _cleanup()


#  3. 语义: 未知 run 404


def test_monitor_run_detail_404():
    with _client() as c:
        r = c.get("/monitor/runs/no-such-run/stages")
        assert r.status_code == 404
