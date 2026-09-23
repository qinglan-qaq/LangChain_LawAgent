"""方案c — JSON 会话历史落 PG(session_dialogue_events 事件流 + 聚合)。

真实 PG(不可用显式 SKIP, 不 mock —— 降级用例除外), 风格对齐
tests/test_trace_db.py。测试用独立 T-dialogue-test* 会话, 结束即清理。
"""
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# psycopg async 在 Windows 需 SelectorEventLoop(与 uvicorn loop 工厂同款)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

_T_PREFIX = "T-dialogue-test"


def _pg_ok() -> bool:
    """独立连接探测 — 不碰全局连接池(对齐 test_trace_db)。"""

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
    """清理测试会话的事件行(独立短连接, 不碰全局池)。"""

    async def _run():
        from psycopg import AsyncConnection

        from lawApp_LangGraph.db import build_dsn

        conn = await AsyncConnection.connect(build_dsn(), autocommit=True)
        try:
            await conn.execute(
                "DELETE FROM session_dialogue_events WHERE session_id LIKE %s",
                (_T_PREFIX + "%",),
            )
        finally:
            await conn.close()

    try:
        asyncio.run(_run())
    except Exception:
        pass  # PG 不可用 → 后续用例各自 SKIP


def _skip_if_no_pg():
    if not _pg_ok():
        import pytest

        pytest.skip("PG 不可用, 显式跳过(不 mock)")


#  1. 建表存在性


def test_dialogue_events_table_exists():
    _skip_if_no_pg()

    async def _run():
        from lawApp_LangGraph.db import close_pool, ensure_tables, get_pool

        pool = await get_pool()
        async with pool.connection() as conn:
            await ensure_tables(conn)  # 幂等 DDL(含新表)
            cur = await conn.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_name = 'session_dialogue_events'"
            )
            (n,) = await cur.fetchone()
            assert n == 1, "session_dialogue_events 表必须在 Law_app 库可查"
        await close_pool()

    _cleanup()
    try:
        asyncio.run(_run())
    finally:
        _cleanup()


#  2. log_event 原子 seq + dedupe 幂等


def test_log_event_atomic_seq_and_dedupe():
    _skip_if_no_pg()
    from lawApp_LangGraph import dialogue_log

    sid = f"{_T_PREFIX}-1"

    async def _run():
        dialogue_log.log_event(
            sid, "round_question",
            {"round": 1, "question": "是否有未成年子女?",
             "options": ["有一个孩子", "无子女"]},
        )
        dialogue_log.log_event(
            sid, "round_answer",
            {"round": 1, "selected": "有一个孩子", "selected_type": "option"},
        )
        dialogue_log.log_event(
            sid, "interrupt_confirm",
            {"type": "risk_confirm", "question": None, "chosen": "继续咨询"},
        )
        # dedupe: 同 session + 同类型 + payload 覆盖 (round, question) → 跳过
        # (模拟 resume 重跑节点时 interrupt 前写入再次执行)
        dialogue_log.log_event(
            sid, "round_question",
            {"round": 1, "question": "是否有未成年子女?",
             "options": ["有一个孩子", "无子女"]},
            dedupe_on=("round", "question"),
        )

        from lawApp_LangGraph.db import close_pool, get_pool

        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT seq, event_type, payload FROM session_dialogue_events "
                "WHERE session_id = %s ORDER BY seq",
                (sid,),
            )
            rows = await cur.fetchall()
        await close_pool()

        # dedupe 生效: 重复的 round_question 不再追加
        assert [r[0] for r in rows] == [1, 2, 3], "seq 必须连续 1/2/3"
        assert [r[1] for r in rows] == [
            "round_question", "round_answer", "interrupt_confirm",
        ]
        assert rows[0][2]["question"] == "是否有未成年子女?"
        assert rows[0][2]["options"] == ["有一个孩子", "无子女"]
        assert rows[2][2]["chosen"] == "继续咨询"

    _cleanup()
    try:
        asyncio.run(_run())
    finally:
        _cleanup()


#  3. aggregate_dialogue 拼装(配对 / free_text 分支 / 只有问没答 / 空态)


def test_aggregate_dialogue_assembles_contract():
    _skip_if_no_pg()
    from lawApp_LangGraph import dialogue_log

    sid = f"{_T_PREFIX}-agg"

    async def _run():
        # 轮1: 选项命中(option 分支)
        dialogue_log.log_event(
            sid, "round_question",
            {"round": 1, "question": "是否有未成年子女?",
             "options": ["有一个孩子", "两个孩子", "无子女"]},
        )
        dialogue_log.log_event(
            sid, "round_answer",
            {"round": 1, "question": "是否有未成年子女?",
             "options": ["有一个孩子", "两个孩子", "无子女"],
             "selected": "有一个孩子", "selected_type": "option",
             "free_text": None, "element_keys": ["children"]},
        )
        # 轮2: 自由输入(free_text 分支)
        dialogue_log.log_event(
            sid, "round_question",
            {"round": 2, "question": "核心诉求是什么?", "options": []},
        )
        dialogue_log.log_event(
            sid, "round_answer",
            {"round": 2, "question": "核心诉求是什么?", "options": [],
             "selected": "想争取抚养权", "selected_type": "free_text",
             "free_text": "想争取抚养权", "element_keys": ["demand"]},
        )
        # 轮3: 只有问没答(用户停在 interrupt)
        dialogue_log.log_event(
            sid, "round_question",
            {"round": 3, "question": "婚房是婚前还是婚后购买?",
             "options": ["婚前", "婚后"]},
        )
        dialogue_log.log_event(
            sid, "interrupt_confirm",
            {"type": "budget_confirm", "question": "材料仍不充分", "chosen": "同意"},
        )
        dialogue_log.log_event(
            sid, "final_answer",
            {"answer": "建议先协商抚养权归属。",
             "citations": ["民法典 第一千零八十四条"], "clarify_rounds": 3},
        )
        doc = await dialogue_log.aggregate_dialogue(sid)
        from lawApp_LangGraph.db import close_pool

        await close_pool()

        assert doc["session_id"] == sid
        assert len(doc["rounds"]) == 3
        r1, r2, r3 = doc["rounds"]
        # option 分支
        assert r1["round"] == 1
        assert r1["question"] == "是否有未成年子女?"
        assert r1["options"] == ["有一个孩子", "两个孩子", "无子女"]
        assert r1["selected"] == "有一个孩子"
        assert r1["selected_type"] == "option"
        assert r1["free_text"] is None
        assert r1["element_keys"] == ["children"]
        # free_text 分支
        assert r2["selected_type"] == "free_text"
        assert r2["free_text"] == "想争取抚养权"
        assert r2["element_keys"] == ["demand"]
        # 只有问没答 → selected=null 单独成条
        assert r3["selected"] is None
        assert r3["selected_type"] is None
        assert r3["free_text"] is None
        # ts 均为 ISO 字符串
        for r in doc["rounds"]:
            assert isinstance(r["ts"], str) and "T" in r["ts"]
        # confirms / final
        (c,) = doc["confirms"]
        assert c["type"] == "budget_confirm"
        assert c["question"] == "材料仍不充分"
        assert c["chosen"] == "同意"
        assert isinstance(c["ts"], str)
        assert doc["final"]["answer"] == "建议先协商抚养权归属。"
        assert doc["final"]["citations"] == ["民法典 第一千零八十四条"]
        assert doc["final"]["clarify_rounds"] == 3
        assert isinstance(doc["final"]["ts"], str)

    _cleanup()
    try:
        asyncio.run(_run())
    finally:
        _cleanup()


def test_aggregate_dialogue_empty_session():
    _skip_if_no_pg()

    async def _run():
        from lawApp_LangGraph import dialogue_log
        from lawApp_LangGraph.db import close_pool

        doc = await dialogue_log.aggregate_dialogue(f"{_T_PREFIX}-nonexist")
        await close_pool()
        assert doc == {
            "session_id": f"{_T_PREFIX}-nonexist",
            "rounds": [],
            "confirms": [],
            "final": None,
            "docx": None,
        }

    asyncio.run(_run())


#  4. 降级: 写入失败不打断图流程


def test_log_event_failure_never_raises(monkeypatch, caplog):
    from lawApp_LangGraph import dialogue_log

    def _boom():
        raise RuntimeError("PG 掉线(模拟)")

    monkeypatch.setattr(dialogue_log, "_connect", _boom)
    with caplog.at_level(logging.WARNING, logger="lawApp.dialogue"):
        # 直接调用不抛(全路径吞异常)
        dialogue_log.log_event(f"{_T_PREFIX}-degrade", "round_question", {"round": 1})
        # 空 session_id(无 config 的直调场景)→ 静默跳过
        dialogue_log.log_event("", "round_question", {"round": 1})
    assert any(
        "写入失败" in r.getMessage() for r in caplog.records
    ), "写入失败必须留下 warning 痕迹"


def test_ask_element_node_survives_dialogue_db_down(monkeypatch):
    """图节点写入点在 PG 掉线时不打断节点执行(方案c 铁律)。"""
    import lawApp_LangGraph.LangGraph_lawApp as app
    from lawApp_LangGraph import dialogue_log
    from lawApp_LangGraph.state import AgentState, ElementQuestion

    def _boom():
        raise RuntimeError("PG 掉线(模拟)")

    monkeypatch.setattr(dialogue_log, "_connect", _boom)

    def fake_interrupt(payload):
        return "结婚5年"

    monkeypatch.setattr(app, "interrupt", fake_interrupt, raising=True)

    state = AgentState(
        query="我想离婚",
        pending_questions=[
            ElementQuestion(
                key="marriage_status", question="请问结婚多少年了?",
                options=["不到3年", "3到10年"],
            )
        ],
    )
    # 带 config(thread_id 即 session_id)直调: 两个写入点均触发且均降级放行
    out = app.ask_element_node(
        state, {"configurable": {"thread_id": f"{_T_PREFIX}-node"}}
    )
    assert out["clarify_history"][0].answer == "结婚5年"
    assert out["clarify_rounds"] == 1


#  5. API 端点: 非法 sid 400 / 空态 200 / PG 掉线 200 空结构


def test_dialogue_endpoint_rejects_invalid_sid():
    from fastapi.testclient import TestClient

    from lawApp_LangGraph.FastAPI.api import app

    with TestClient(app) as client:
        # 带模式前缀但格式非法 → 400
        r = client.get("/sessions/AT-bad-format/dialogue")
        assert r.status_code == 400
        assert r.json()["detail"] == "invalid_session_id"
        # 纯空白 sid(strip 后为空)→ 400
        r2 = client.get("/sessions/%20%20/dialogue")
        assert r2.status_code == 400


def test_dialogue_endpoint_returns_contract():
    """真实 PG: 有事件 → 契约字段齐全; 空会话 → 200 空结构。"""
    _skip_if_no_pg()
    from fastapi.testclient import TestClient

    from lawApp_LangGraph import dialogue_log
    from lawApp_LangGraph.FastAPI.api import app

    sid = f"{_T_PREFIX}-api"
    _cleanup()
    dialogue_log.log_event(
        sid, "round_question",
        {"round": 1, "question": "是否有未成年子女?",
         "options": ["有一个孩子", "两个孩子", "无子女"]},
    )
    dialogue_log.log_event(
        sid, "round_answer",
        {"round": 1, "question": "是否有未成年子女?",
         "options": ["有一个孩子", "两个孩子", "无子女"],
         "selected": "有一个孩子", "selected_type": "option",
         "free_text": None, "element_keys": ["children"]},
    )
    dialogue_log.log_event(
        sid, "final_answer",
        {"answer": "建议先协商。", "citations": [], "clarify_rounds": 1},
    )
    try:
        with TestClient(app) as client:
            r = client.get(f"/sessions/{sid}/dialogue")
            assert r.status_code == 200
            body = r.json()
            assert body["session_id"] == sid
            assert body["rounds"][0]["selected"] == "有一个孩子"
            assert body["rounds"][0]["selected_type"] == "option"
            assert body["confirms"] == []
            assert body["final"]["clarify_rounds"] == 1

            # 空会话 → 200 空结构(前端好处理)
            r2 = client.get("/sessions/AT-20260923-101530-001/dialogue")
            assert r2.status_code == 200
            assert r2.json() == {
                "session_id": "AT-20260923-101530-001",
                "rounds": [],
                "confirms": [],
                "final": None,
                "docx": None,
            }
    finally:
        _cleanup()


def test_dialogue_endpoint_degrades_when_pg_down(monkeypatch):
    """PG 掉线 → 200 空结构 + ERROR 日志(对齐 GET /sessions 降级先例)。"""
    from fastapi.testclient import TestClient

    import lawApp_LangGraph.db as db
    from lawApp_LangGraph.FastAPI.api import app

    async def _boom():
        raise RuntimeError("PG 掉线(模拟)")

    monkeypatch.setattr(db, "get_pool", _boom)

    class _Capture(logging.Handler):
        def __init__(self):
            super().__init__()
            self.records: list[str] = []

        def emit(self, record):
            self.records.append(record.getMessage())

    cap = _Capture()
    dlg_logger = logging.getLogger("lawApp.dialogue")
    with TestClient(app) as client:
        dlg_logger.addHandler(cap)
        try:
            r = client.get("/sessions/AT-20260923-101530-001/dialogue")
        finally:
            dlg_logger.removeHandler(cap)

    assert r.status_code == 200
    assert r.json() == {
        "session_id": "AT-20260923-101530-001",
        "rounds": [],
        "confirms": [],
        "final": None,
        "docx": None,
    }
    assert any("读取降级" in m for m in cap.records), "降级必须留下 ERROR 日志痕迹"
