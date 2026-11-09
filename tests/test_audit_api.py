"""审计端点契约 — 真实 PG; 不可用显式 skip(不 mock)."""
from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

_P = "api-audit"


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


def _cleanup() -> None:
    async def _run():
        from psycopg import AsyncConnection

        from lawApp_LangGraph.db import build_dsn

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


def _seed_audit() -> dict:
    if not _pg_ok():
        pytest.skip("PG 不可用, 显式跳过(不 mock)")

    from psycopg import AsyncConnection

    from lawApp_LangGraph.db import build_dsn
    from lawApp_LangGraph.evidence import evidence_id_for

    stamp = time.time_ns()
    rid_p = f"{_P}-parent-{stamp}"
    rid_c = f"{_P}-child-{stamp}"
    t0 = datetime(2026, 11, 8, 10, 0, 0, tzinfo=timezone.utc)
    eid = evidence_id_for(rid_c, "case", "（2023）京01民终1号")
    out = {"stage_fin": 0, "stage_tool": 0}

    async def run():
        conn = await AsyncConnection.connect(build_dsn(), autocommit=True)
        try:
            await conn.execute(
                "INSERT INTO trace_runs (run_id, session_id, run_type, mode, "
                "status, query, final_answer, metrics, started_at, ended_at, "
                "parent_run_id, current_node) VALUES "
                "(%s,'sess-audit','live_ask','attorney','interrupted','离婚','',"
                "'{}',%s,%s,NULL,'ask_element')",
                (rid_p, t0, t0 + timedelta(milliseconds=500)),
            )
            await conn.execute(
                "INSERT INTO trace_runs (run_id, session_id, run_type, mode, "
                "status, query, final_answer, metrics, started_at, ended_at, "
                "parent_run_id, current_node) VALUES "
                "(%s,'sess-audit','live_resume','attorney','ok','离婚','终答',"
                "'{}',%s,%s,%s,'finalize')",
                (
                    rid_c,
                    t0 + timedelta(seconds=5),
                    t0 + timedelta(seconds=6),
                    rid_p,
                ),
            )
            cur = await conn.execute(
                "INSERT INTO stage_chain (run_id, session_id, node_name, seq, "
                "status, started_at, ended_at, latency_ms, detail) VALUES "
                "(%s,'sess-audit','ask_element',1,'interrupted',%s,%s,100,'{}') "
                "RETURNING id",
                (rid_p, t0, t0 + timedelta(milliseconds=100)),
            )
            stage_ask = (await cur.fetchone())[0]
            await conn.execute(
                "INSERT INTO minor_states (stage_id, attempt, phase, input_text, "
                "output_text, output_json, context_meta, metrics, refs, "
                "content_source) VALUES (%s,1,'waiting_human','问要素',"
                "'等待用户回答',%s,%s,'{}',%s,'projected')",
                (
                    stage_ask,
                    '{"question": "结婚几年?"}',
                    '{"current_query": {"chars": 2, "hash": "aa"}}',
                    '{}',
                ),
            )

            cur = await conn.execute(
                "INSERT INTO stage_chain (run_id, session_id, node_name, seq, "
                "status, started_at, ended_at, latency_ms, detail) VALUES "
                "(%s,'sess-audit','tools',1,'ok',%s,%s,90,'{}') RETURNING id",
                (
                    rid_c,
                    t0 + timedelta(seconds=5),
                    t0 + timedelta(milliseconds=5090),
                ),
            )
            out["stage_tool"] = (await cur.fetchone())[0]
            await conn.execute(
                "INSERT INTO minor_states (stage_id, attempt, phase, input_text, "
                "output_text, output_json, context_meta, metrics, refs, "
                "content_source) VALUES (%s,1,'done','检索','案例 1 条',%s,%s,"
                "%s,%s,'projected')",
                (
                    out["stage_tool"],
                    '{"tool": "retrieve_legal_knowledge", "doc_count": 1}',
                    '{"current_query": {"chars": 2, "hash": "aa"}, '
                    '"evidence": {"chars": 80, "hash": "bb"}}',
                    '{"tokens": {"prompt": 10, "completion": 5, "total": 15}}',
                    json.dumps(
                        {"evidence_ids": [eid], "evidence_ids_used": [eid]}
                    ),
                ),
            )
            await conn.execute(
                "INSERT INTO trace_spans (run_id, span_type, name, status, "
                "input, output, state, latency_ms, token_usage, started_at) "
                "VALUES (%s,'tool','retrieve_legal_knowledge','ok',%s,%s,NULL,"
                "90,%s,%s)",
                (
                    rid_c,
                    '{"query": "离婚"}',
                    json.dumps(
                        {
                            "rag_documents": [
                                {
                                    "case_number": "（2023）京01民终1号",
                                    "relevance_score": 0.8,
                                    "chunk_text": "案情摘要",
                                }
                            ],
                            "evaluation": {"quality_verdict": "相关"},
                        },
                        ensure_ascii=False,
                    ),
                    '{"prompt": 10, "completion": 5}',
                    t0 + timedelta(seconds=5),
                ),
            )

            cur = await conn.execute(
                "INSERT INTO stage_chain (run_id, session_id, node_name, seq, "
                "status, started_at, ended_at, latency_ms, detail) VALUES "
                "(%s,'sess-audit','finalize',1,'ok',%s,%s,150,'{}') RETURNING id",
                (
                    rid_c,
                    t0 + timedelta(milliseconds=5100),
                    t0 + timedelta(seconds=6),
                ),
            )
            out["stage_fin"] = (await cur.fetchone())[0]
            await conn.execute(
                "INSERT INTO minor_states (stage_id, attempt, phase, input_text, "
                "output_text, output_json, context_meta, metrics, refs, "
                "content_source) VALUES (%s,1,'done','生成终答','终答',%s,%s,"
                "'{}',%s,'projected')",
                (
                    out["stage_fin"],
                    json.dumps(
                        {
                            "answer_len": 2,
                            "citations": [
                                {
                                    "label": "（2023）京01民终1号",
                                    "type": "case",
                                    "evidence_ids": [eid],
                                    "resolved": True,
                                }
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    '{"current_query": {"chars": 2, "hash": "aa"}, '
                    '"history": {"chars": 10, "hash": "cc", "turns": 1}}',
                    json.dumps({"citation_evidence_ids": [eid]}),
                ),
            )
        finally:
            await conn.close()

    asyncio.run(run())
    out.update({"parent": rid_p, "child": rid_c})
    return out


def _client():
    from fastapi.testclient import TestClient

    from lawApp_LangGraph.FastAPI.api import app

    return TestClient(app)


def test_audit_view_returns_schema_and_chain():
    ids = _seed_audit()
    try:
        with _client() as c:
            r = c.get(f"/monitor/runs/{ids['child']}/audit-view?chain=1")
            assert r.status_code == 200
            body = r.json()
            assert body["schema_version"] == 1
            assert body["run"]["run_id"] == ids["child"]
            assert len(body["chain"]) == 2
            assert body["verdicts"]["flow"]["level"] in {
                "good", "warn", "bad", "unknown", "na"
            }
            assert {"tool_calls", "documents", "citations", "gaps"} <= set(
                body["evidence"]
            )
            assert len(body["evidence"]["documents"]) == 1
            assert body["evidence"]["documents"][0]["used_in_analysis"] is True
            assert body["timeline"] and "performance" in body
    finally:
        _cleanup()


def test_audit_view_after_stage_id_filters_timeline():
    ids = _seed_audit()
    try:
        with _client() as c:
            body = c.get(
                f"/monitor/runs/{ids['child']}/audit-view?after_stage_id="
                f"{ids['stage_tool']}"
            ).json()
            names = [n for g in body["timeline"] for n in g["node_names"]]
            assert "finalize" in names
            assert "tools" not in names
    finally:
        _cleanup()


def test_audit_detail_hides_raw_by_default():
    ids = _seed_audit()
    try:
        with _client() as c:
            body = c.get(
                f"/monitor/stages/{ids['stage_fin']}/audit-detail"
            ).json()
            assert not body.get("raw")
            assert "final_prompts" not in json.dumps(body, ensure_ascii=False)
            assert body["node"]["node_name"] == "finalize"
            raw = c.get(
                f"/monitor/stages/{ids['stage_fin']}/audit-detail?include_raw=1"
            ).json()
            assert raw["raw"]["minor_state"]["node_name"] == "finalize"
    finally:
        _cleanup()


def test_audit_view_and_detail_404():
    if not _pg_ok():
        pytest.skip("PG 不可用, 显式跳过(不 mock)")
    with _client() as c:
        assert c.get(
            "/monitor/runs/no-such-run/audit-view"
        ).status_code == 404
        assert c.get(
            "/monitor/stages/999999999/audit-detail"
        ).status_code == 404
