"""
会话对话历史落 PG — 方案c: 事件流表(session_dialogue_events)+ 聚合 API

职责:
    - log_event: 节点内原子写一行事件(round_question / round_answer /
      interrupt_confirm / final_answer), seq 由单条 SQL 内 MAX(seq)+1 生成
    - fetch_dialogue / aggregate_dialogue: 事件流读取与拼装, 供
      GET /sessions/{sid}/dialogue 聚合端点返回会话级文档契约

铁律(对齐 db.record_audit / api._safe_audit 先例):
    写入/读取失败绝不阻断图流程 —— 全路径吞异常记 warning, 不抛。
    节点多为 async 上下文但含同步节点(ask_element / hitl_degrade /
    hitl_budget), 且单条 INSERT 毫秒级 —— 统一用 psycopg 同步短连接
    (对齐 utils._seed_daily_seq 的同步查询模式), 不碰全局 async 池。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from lawApp_LangGraph import db

# 标准库 logger(观测旁路告警; FastAPI.logging 的 flow/system 包装
# propagate=False, 不适合旁路告警 —— 对齐 db.py "lawApp.db" 惯例)
logger = logging.getLogger("lawApp.dialogue")


def _connect():
    """同步短连接(autocommit): 单条 INSERT/SELECT 用完即弃。

    独立成函数便于测试 monkeypatch(降级用例替换为抛异常)。
    """
    import psycopg

    return psycopg.connect(db.build_dsn(), autocommit=True)


def _payload_json(payload: dict) -> str:
    """payload 序列化: 非 JSON 原生类型 default=str 兜住(对齐 record_audit)。"""
    return json.dumps(payload or {}, ensure_ascii=False, default=str)


def log_event(
    session_id: str,
    event_type: str,
    payload: dict,
    dedupe_on: Optional[tuple] = None,
) -> None:
    """原子写一行对话事件; 失败仅记 warning, 绝不抛(图流程优先)。

    Args:
        session_id: 会话 ID(LangGraph thread_id); 空(None/缺省) → 静默跳过
            (无 config 的直调场景, 如测试替身)
        event_type: round_question / round_answer / interrupt_confirm /
            final_answer
        payload: 事件载荷(dict, JSONB 落库)
        dedupe_on: payload 键元组 —— 给定时先查同 session + 同 event_type +
            payload @> {指定键子集} 是否已存在, 存在则跳过。用于 round_question:
            interrupt 暂停后 resume 会从头重跑节点, interrupt 前的写入
            会执行两次, 靠该守卫保持幂等(节点 resume 重跑幂等的既有语义)。
    """
    try:
        if not session_id:
            return
        with _connect() as conn:
            if dedupe_on:
                probe = _payload_json({k: (payload or {}).get(k) for k in dedupe_on})
                cur = conn.execute(
                    "SELECT 1 FROM session_dialogue_events "
                    "WHERE session_id = %s AND event_type = %s "
                    "AND payload @> %s::jsonb LIMIT 1",
                    (session_id, event_type, probe),
                )
                if cur.fetchone():
                    return
            conn.execute(
                """
                INSERT INTO session_dialogue_events
                    (session_id, seq, event_type, payload)
                VALUES (%s,
                        (SELECT COALESCE(MAX(seq), 0) + 1
                         FROM session_dialogue_events WHERE session_id = %s),
                        %s, %s::jsonb)
                """,
                (session_id, session_id, event_type, _payload_json(payload)),
            )
    except Exception as e:  # pragma: no cover — 对话历史旁路失败不阻断主流程
        logger.warning("dialogue 事件写入失败: %s", e)


async def fetch_dialogue(session_id: str) -> list[dict]:
    """按 seq 升序读会话事件流; 异常返回 [](不抛, 对齐 GET /sessions 降级先例)。"""
    try:
        pool = await db.get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT seq, event_type, payload, created_at "
                "FROM session_dialogue_events WHERE session_id = %s ORDER BY seq",
                (session_id,),
            )
            rows = await cur.fetchall()
        return [
            {
                "seq": r[0],
                "event_type": r[1],
                "payload": r[2] or {},
                "created_at": r[3],
            }
            for r in rows
        ]
    except Exception as e:
        # 读取失败影响响应内容(降级空结构)→ ERROR(对齐 GET /sessions 降级先例)
        logger.error("对话历史读取降级: %s", e)
        return []


def _iso(ts: Any) -> Optional[str]:
    """created_at(timestamptz datetime)→ ISO 字符串; 非法值 → None。"""
    try:
        return ts.isoformat() if ts is not None else None
    except Exception:
        return None


async def aggregate_dialogue(session_id: str) -> dict:
    """把事件流拼成会话级文档契约(GET /sessions/{sid}/dialogue 响应体)。

    拼装规则:
        - round_question 与 round_answer 按 payload.round 配对(seq 顺序:
          每个 question 取其后首个未被消费的相同 round 的 answer),
          只有问没答的(用户停在 interrupt / 跳过)单独成条 selected=null
        - interrupt_confirm 逐条入 confirms(docx_confirm 同构映射, 结构化键
          多透 filled/pending/critical_missing 供前端拼决策行)
        - final_answer 取最后一条入 final(多轮对话以最新终答为准)
        - docx_generated 取最后一条入 docx(多次生成以最新文书为准;
          无 docx 事件的会话为 None, 前端据此隐藏下载入口)

    Returns:
        dict: {"session_id", "rounds": [], "confirms": [], "final": None,
        "docx": None} 形状, 空态合法(rounds/confirms 空列表, final/docx None)。
    """
    events = await fetch_dialogue(session_id)
    rounds: list[dict] = []
    confirms: list[dict] = []
    final: Optional[dict] = None
    docx: Optional[dict] = None
    consumed: set[int] = set()  # 已配对的 round_answer seq(防跨轮重复消费)

    for ev in events:
        et = ev["event_type"]
        p = ev["payload"] or {}
        ts = _iso(ev.get("created_at"))
        if et == "round_question":
            answer = None
            for cand in events:
                if (
                    cand["seq"] in consumed
                    or cand["seq"] <= ev["seq"]
                    or cand["event_type"] != "round_answer"
                    or (cand["payload"] or {}).get("round") != p.get("round")
                ):
                    continue
                answer = cand
                break
            if answer is not None:
                consumed.add(answer["seq"])
                ap = answer["payload"] or {}
                rounds.append(
                    {
                        "round": p.get("round"),
                        "question": p.get("question"),
                        "options": p.get("options") or [],
                        "selected": ap.get("selected"),
                        "selected_type": ap.get("selected_type"),
                        "free_text": ap.get("free_text"),
                        "element_keys": ap.get("element_keys") or [],
                        "ts": _iso(answer.get("created_at")),
                    }
                )
            else:
                rounds.append(
                    {
                        "round": p.get("round"),
                        "question": p.get("question"),
                        "options": p.get("options") or [],
                        "selected": None,
                        "selected_type": None,
                        "free_text": None,
                        "element_keys": [],
                        "ts": ts,
                    }
                )
        elif et == "interrupt_confirm":
            confirms.append(
                {
                    "type": p.get("type"),
                    "question": p.get("question"),
                    "chosen": p.get("chosen"),
                    "ts": ts,
                }
            )
        elif et == "docx_confirm":
            # SF-3(spec §5 缺口): docx 确认决策同构映射入 confirms(结构化键直透,
            # 展示文本由前端拼), 律师确认/跳过决策在时间线可见
            confirms.append(
                {
                    "type": "docx_confirm",
                    "question": p.get("question"),
                    "chosen": p.get("chosen"),
                    "filled": p.get("filled"),
                    "pending": p.get("pending"),
                    "critical_missing": p.get("critical_missing") or [],
                    "ts": ts,
                }
            )
        elif et == "final_answer":
            final = {
                "answer": p.get("answer"),
                "citations": p.get("citations") or [],
                "clarify_rounds": p.get("clarify_rounds"),
                "ts": ts,
            }
        elif et == "docx_generated":
            # seq 升序遍历, 后者覆盖前者 → 自然取最后一条(最新文书)
            docx = {
                "path": p.get("docx_path"),
                "filled": p.get("filled"),
                "pending": p.get("pending"),
            }

    return {
        "session_id": session_id,
        "rounds": rounds,
        "confirms": confirms,
        "final": final,
        "docx": docx,
    }
