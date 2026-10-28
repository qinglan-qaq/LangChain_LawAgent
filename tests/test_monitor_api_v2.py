"""monitor v2 端点 — timeline / minor-states / stage detail / config(真实 PG)。"""
from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

_P = "api-mon-v2"


def _pg_ok() -> bool:
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
    async def _run():
        from lawApp_LangGraph.db import build_dsn
        from psycopg import AsyncConnection

        conn = await AsyncConnection.connect(build_dsn(), autocommit=True)
        try:
            for table in ("trace_spans", "stage_chain", "trace_runs"):
                await conn.execute(
                    f"DELETE FROM {table} WHERE run_id LIKE %s", (_P + "%",)
                )
        finally:
            await conn.close()

    try:
        asyncio.run(_run())
    except Exception:
        pass


def _seed() -> dict:
    if not _pg_ok():
        pytest.skip("PG 不可用, 显式跳过(不 mock)")

    from psycopg import AsyncConnection

    from lawApp_LangGraph.db import build_dsn

    rid_p = f"{_P}-parent-{time.time_ns()}"
    rid_c = f"{_P}-child-{time.time_ns()}"
    t0 = datetime(2026, 10, 23, 10, 0, 0, tzinfo=timezone.utc)

    stages: dict[str, int] = {}

    async def run():
        conn = await AsyncConnection.connect(build_dsn(), autocommit=True)
        try:
            await conn.execute(
                "INSERT INTO trace_runs (run_id, session_id, run_type, mode, "
                "status, query, final_answer, metrics, started_at, ended_at, "
                "parent_run_id, current_node) VALUES "
                "(%s,'sess-v2','live_ask','attorney','interrupted','q1','',"
                "'{}',%s,%s,NULL,'ask_element')",
                (rid_p, t0, t0 + timedelta(milliseconds=200)),
            )
            await conn.execute(
                "INSERT INTO trace_runs (run_id, session_id, run_type, mode, "
                "status, query, final_answer, metrics, started_at, ended_at, "
                "parent_run_id, current_node) VALUES "
                "(%s,'sess-v2','live_resume','attorney','ok','q1','终答',"
                "'{}',%s,%s,%s,'finalize')",
                (rid_c, t0 + timedelta(seconds=5),
                 t0 + timedelta(milliseconds=5300), rid_p),
            )
            # parent: ask_element(waiting_human) + planner(ok)
            cur = await conn.execute(
                "INSERT INTO stage_chain (run_id, session_id, node_name, seq, "
                "status, started_at, ended_at, latency_ms, detail) VALUES "
                "(%s,'sess-v2','ask_element',1,'interrupted',%s,%s,100,'{}') "
                "RETURNING id",
                (rid_p, t0, t0 + timedelta(milliseconds=100)),
            )
            stage_ask = (await cur.fetchone())[0]
            stages["ask"] = stage_ask
            await conn.execute(
                "INSERT INTO minor_states (stage_id, attempt, phase, input_text, "
                "output_text, output_json, context_meta, metrics, route, "
                "content_source) VALUES (%s,1,'waiting_human','问要素',"
                "'等待用户回答',%s,%s,'{}',NULL,'projected')",
                (
                    stage_ask,
                    '{"question": "结婚几年?", "answer": "结婚5年"}',
                    '{"current_query": {"chars": 2}}',
                ),
            )
            cur = await conn.execute(
                "INSERT INTO stage_chain (run_id, session_id, node_name, seq, "
                "status, started_at, ended_at, latency_ms, detail) VALUES "
                "(%s,'sess-v2','planner',1,'ok',%s,%s,80,'{}') RETURNING id",
                (rid_p, t0 + timedelta(milliseconds=100),
                 t0 + timedelta(milliseconds=200)),
            )
            stage_plan_p = (await cur.fetchone())[0]
            stages["plan_p"] = stage_plan_p
            await conn.execute(
                "INSERT INTO minor_states (stage_id, phase, input_text, "
                "output_text, output_json, content_source) "
                "VALUES (%s,'done','in','计划',%s,'projected')",
                (stage_plan_p, '{"step_count": 1}'),
            )
            # child: ingest 无 minor 行(占位) + finalize 有内容
            cur = await conn.execute(
                "INSERT INTO stage_chain (run_id, session_id, node_name, seq, "
                "status, started_at, ended_at, latency_ms, detail) VALUES "
                "(%s,'sess-v2','ingest',1,'ok',%s,%s,20,'{}') RETURNING id",
                (rid_c, t0 + timedelta(seconds=5),
                 t0 + timedelta(milliseconds=5020)),
            )
            stage_ingest_c = (await cur.fetchone())[0]
            stages["ingest_c"] = stage_ingest_c
            cur = await conn.execute(
                "INSERT INTO stage_chain (run_id, session_id, node_name, seq, "
                "status, started_at, ended_at, latency_ms, detail) VALUES "
                "(%s,'sess-v2','finalize',1,'ok',%s,%s,150,'{}') RETURNING id",
                (rid_c, t0 + timedelta(milliseconds=5150),
                 t0 + timedelta(milliseconds=5300)),
            )
            stage_fin_c = (await cur.fetchone())[0]
            stages["fin_c"] = stage_fin_c
            await conn.execute(
                "INSERT INTO minor_states (stage_id, phase, input_text, "
                "output_text, output_json, content_source) "
                "VALUES (%s,'done','in','终答',%s,'projected')",
                (stage_fin_c, '{"answer_len": 2}'),
            )
            await conn.execute(
                "INSERT INTO trace_spans (run_id, span_type, name, status, "
                "input, output, state, latency_ms, token_usage, started_at) "
                "VALUES (%s,'node','finalize','ok',%s,%s,NULL,150,NULL,%s)",
                (rid_c, '{"nodes": 1}', '{"answer": "终答"}',
                 t0 + timedelta(milliseconds=5150)),
            )
        finally:
            await conn.close()

    asyncio.run(run())
    return {"parent": rid_p, "child": rid_c,
            "stage_ask": stages["ask"], "stage_plan_p": stages["plan_p"],
            "stage_ingest_c": stages["ingest_c"], "stage_fin_c": stages["fin_c"]}


def _client():
    from fastapi.testclient import TestClient

    from lawApp_LangGraph.FastAPI.api import app

    return TestClient(app)


def test_timeline_chain_and_placeholder():
    ids = _seed()
    try:
        with _client() as c:
            r = c.get(f"/monitor/runs/{ids['child']}/timeline?chain=1")
            assert r.status_code == 200
            body = r.json()
            assert len(body["runs"]) == 2, "chain=1 串起 parent"
            assert [x["run_id"] for x in body["runs"]] == [
                ids["parent"], ids["child"]
            ]
            kinds = [x["kind"] for x in body["items"]]
            assert kinds.count("divider") == 1
            assert kinds.count("node") == 4
            divider = next(x for x in body["items"] if x["kind"] == "divider")
            assert divider["text"] == "用户回答: 结婚5年"

            ingest = next(
                x for x in body["items"] if x["state"]
                and x["state"]["node_name"] == "ingest"
            )
            assert ingest["state"]["content_source"] == "fallback_placeholder"
            assert ingest["state"]["output_json"]["reason"] == "minor_row_missing"
            assert ingest["state"]["phase"] == "done"
            assert ingest["percent_of_run"] is not None

            total = body["total_composition"]
            assert total["wall_clock_ms"] >= 5000
            assert total["execution_ms"] == 350
            assert total["wait_human_ms"] >= 4800
            assert total["overhead_ms"] >= 0
            assert 0 <= total["execution_pct"] <= 100
    finally:
        _cleanup()


def test_timeline_without_chain_only_current_run():
    ids = _seed()
    try:
        with _client() as c:
            body = c.get(f"/monitor/runs/{ids['child']}/timeline").json()
            assert len(body["runs"]) == 1
            assert body["runs"][0]["run_id"] == ids["child"]
            assert all(x["kind"] == "node" for x in body["items"])
    finally:
        _cleanup()


def test_minor_states_incremental_and_404():
    ids = _seed()
    try:
        with _client() as c:
            r = c.get(f"/monitor/runs/{ids['child']}/minor-states")
            assert r.status_code == 200
            rows = r.json()
            assert len(rows) == 2
            assert rows[0]["node_name"] == "ingest"
            assert rows[0]["content_source"] == "fallback_placeholder"
            assert rows[1]["node_name"] == "finalize"
            assert rows[1]["output_json"]["answer_len"] == 2

            last = rows[-1]["stage_id"]
            r2 = c.get(
                f"/monitor/runs/{ids['child']}/minor-states?after_stage_id={last}"
            )
            assert r2.status_code == 200 and r2.json() == []

            assert c.get(
                "/monitor/runs/no-such-run/minor-states"
            ).status_code == 404
            assert c.get(
                "/monitor/runs/no-such-run/timeline"
            ).status_code == 404
    finally:
        _cleanup()


def test_stage_detail_and_404():
    ids = _seed()
    try:
        with _client() as c:
            r = c.get(f"/monitor/stages/{ids['stage_fin_c']}/detail")
            assert r.status_code == 200
            body = r.json()
            assert body["state"]["node_name"] == "finalize"
            assert body["state"]["output_text"] == "终答"
            assert body["span"]["name"] == "finalize"
            assert body["span"]["output"]["answer"] == "终答"
            assert body["final_answer"] == "终答"

            ask = c.get(f"/monitor/stages/{ids['stage_ask']}/detail").json()
            assert ask["state"]["phase"] == "waiting_human"
            assert ask["state"]["context_meta"]["current_query"]["chars"] == 2

            assert c.get("/monitor/stages/999999999/detail").status_code == 404
    finally:
        _cleanup()


def test_runs_list_has_v2_fields():
    ids = _seed()
    try:
        with _client() as c:
            body = c.get("/monitor/runs?limit=200").json()
            child = next(x for x in body if x["run_id"] == ids["child"])
            assert child["parent_run_id"] == ids["parent"]
            assert child["current_node"] == "finalize"
            assert child["minor_total"] == 1
            assert child["content_available"] is True
            assert child["stale"] is False
    finally:
        _cleanup()


def test_monitor_config_thresholds():
    from lawApp_LangGraph.config import settings

    with _client() as c:
        body = c.get("/monitor/config").json()
        assert body["correct_threshold"] == settings.correct_threshold
        assert body["incorrect_threshold"] == settings.incorrect_threshold
        assert body["minor_text_limit"] == settings.minor_output_text_limit


def test_timeline_chain_capped_at_three_layers():
    """chain=1 最多回溯 3 层(run + 2 个 parent), 更早祖先不返回。"""
    if not _pg_ok():
        pytest.skip("PG 不可用, 显式跳过(不 mock)")

    from psycopg import AsyncConnection

    from lawApp_LangGraph.db import build_dsn

    stamp = time.time_ns()
    rids = [f"{_P}-cap-{stamp}-{i}" for i in range(4)]
    t0 = datetime(2026, 10, 23, 12, 0, 0, tzinfo=timezone.utc)

    async def seed():
        conn = await AsyncConnection.connect(build_dsn(), autocommit=True)
        try:
            for i, rid in enumerate(rids):
                await conn.execute(
                    "INSERT INTO trace_runs (run_id, session_id, run_type, mode, "
                    "status, query, final_answer, metrics, started_at, ended_at, "
                    "parent_run_id) VALUES (%s,'sess-cap','live_ask','attorney',"
                    "'ok','q','a','{}',%s,%s,%s)",
                    (
                        rid,
                        t0 + timedelta(seconds=i * 10),
                        t0 + timedelta(seconds=i * 10 + 1),
                        rids[i - 1] if i else None,
                    ),
                )
                cur = await conn.execute(
                    "INSERT INTO stage_chain (run_id, session_id, node_name, seq, "
                    "status, started_at, ended_at, latency_ms, detail) "
                    "VALUES (%s,'sess-cap','finalize',1,'ok',%s,%s,10,'{}') "
                    "RETURNING id",
                    (rid, t0 + timedelta(seconds=i * 10),
                     t0 + timedelta(seconds=i * 10, milliseconds=10)),
                )
                stage_id = (await cur.fetchone())[0]
                await conn.execute(
                    "INSERT INTO minor_states (stage_id, phase, output_text, "
                    "content_source) VALUES (%s,'done','ok','projected')",
                    (stage_id,),
                )
        finally:
            await conn.close()

    asyncio.run(seed())
    try:
        with _client() as c:
            body = c.get(f"/monitor/runs/{rids[-1]}/timeline?chain=1").json()
            got = [r["run_id"] for r in body["runs"]]
            assert got == rids[1:], f"应为最近 3 层, 实际 {got}"
            assert rids[0] not in got
            assert len([x for x in body["items"] if x["kind"] == "divider"]) == 2
    finally:
        _cleanup()


def test_monitor_v2_endpoints_503_when_pg_down(monkeypatch):
    """PG 不可用 → 数据端点 503(配置端点不依赖 PG, 仍 200)。"""
    import lawApp_LangGraph.db as db

    async def _boom():
        raise RuntimeError("pg down")

    monkeypatch.setattr(db, "get_pool", _boom)
    with _client() as c:
        assert c.get("/monitor/runs/any/timeline").status_code == 503
        assert c.get("/monitor/runs/any/minor-states").status_code == 503
        assert c.get("/monitor/stages/1/detail").status_code == 503
        assert c.get("/monitor/config").status_code == 200
