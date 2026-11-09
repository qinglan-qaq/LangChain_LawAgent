"""会话全量转录 — checkpoint messages → 轮次; 本地 JSON 导出.

事实源:
- LangGraph checkpoint 的 messages: 全量多轮问答(含每轮完整终答)
- session_dialogue_events: final_answer(citations) / HITL 轮次
- trace_runs: 每轮 run 的 query/final_answer/时间

导出默认只含问答/引用/HITL/要素; 不含 prompts_record、final_prompts、
工具原始参数(内部提示词与上下文不落本地文件). 引用条目优先取事件里的
{label,type,content}, 旧事件只有标题字符串时按当前 state 补正文.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from lawApp_LangGraph.config import settings

logger = logging.getLogger("lawApp.transcript")

_SUPPLEMENT_PREFIX = "[补充]"
_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")
EXPORT_SCHEMA_VERSION = 2


def _msg_type(m: Any) -> str:
    if isinstance(m, dict):
        return str(m.get("type") or m.get("role") or "")
    return str(getattr(m, "type", "") or "")


def _msg_text(m: Any) -> str:
    if isinstance(m, dict):
        return str(m.get("content") or "")
    return str(getattr(m, "content", "") or "")


def _norm(text: str) -> str:
    """比较用归一化: 折叠空白, 不改原文."""
    return " ".join((text or "").split())


def _iso(ts: Any) -> str | None:
    try:
        return ts.isoformat() if ts is not None else None
    except Exception:
        return str(ts) if ts is not None else None


def _new_turn(no: int) -> dict:
    return {
        "turn": no,
        "question": "",
        "supplements": [],
        "answer": "",
        "citations": [],
        "ts": None,
        "run_id": None,
    }


def build_turns(messages: list[Any]) -> list[dict]:
    """human/ai 消息配对成轮次.

    - tool/system 消息忽略(工具 JSON 不是用户原话);
    - 「[补充]」开头的 human 归入当前轮 supplements, 不另起一轮
      (HITL resume 会把补充答案写回 messages);
    - 同一轮出现多条非空 AI 消息时以最后一条为准
      (finalize 幂等/重放场景).
    """
    turns: list[dict] = []
    current: dict | None = None
    for m in messages or []:
        mtype = _msg_type(m)
        text = _msg_text(m).strip()
        if not text or mtype not in ("human", "ai"):
            continue
        if mtype == "human":
            if text.startswith(_SUPPLEMENT_PREFIX):
                if current is None:
                    current = _new_turn(len(turns) + 1)
                    turns.append(current)
                current["supplements"].append(
                    text[len(_SUPPLEMENT_PREFIX) :].strip()
                )
                continue
            current = _new_turn(len(turns) + 1)
            current["question"] = text
            turns.append(current)
        else:
            if current is None:
                current = _new_turn(len(turns) + 1)
                turns.append(current)
            current["answer"] = text
    return [t for t in turns if t["question"] or t["answer"]]


def attach_dialogue(turns: list[dict], events: list[dict]) -> list[dict]:
    """把 dialogue 的 final_answer 事件按答案原文回填引用与时间.

    只做精确匹配(归一化空白): 不存在事件的行(如闲聊)保持空 citations.
    """
    by_answer: dict[str, list[dict]] = {}
    for ev in events or []:
        if (ev.get("event_type") or "") != "final_answer":
            continue
        payload = ev.get("payload") or {}
        answer = str(payload.get("answer") or "").strip()
        if not answer:
            continue
        by_answer.setdefault(_norm(answer), []).append(
            {
                "citations": payload.get("citations") or [],
                "ts": _iso(ev.get("created_at")),
            }
        )
    for turn in turns:
        bucket = by_answer.get(_norm(turn.get("answer") or ""))
        if bucket:
            hit = bucket.pop(0)
            turn["citations"] = hit["citations"]
            turn["ts"] = hit["ts"]
    return turns


def attach_runs(turns: list[dict], runs: list[dict]) -> list[dict]:
    """按问题原文 / 答案原文匹配 run_id; resume run(query 为空)按答案匹配."""
    by_query: dict[str, list[dict]] = {}
    by_answer: dict[str, list[dict]] = {}
    for run in runs or []:
        q = _norm(run.get("query") or "")
        if q:
            by_query.setdefault(q, []).append(run)
        a = _norm(run.get("final_answer") or "")
        if a:
            by_answer.setdefault(a, []).append(run)
    for turn in turns:
        hit = None
        q_bucket = by_query.get(_norm(turn.get("question") or ""))
        if q_bucket:
            hit = q_bucket.pop(0)
        if hit is None:
            a_bucket = by_answer.get(_norm(turn.get("answer") or ""))
            if a_bucket:
                hit = a_bucket.pop(0)
        if hit:
            turn["run_id"] = hit.get("run_id")
            if not turn.get("ts"):
                turn["ts"] = _iso(hit.get("started_at"))
    return turns


def _field(obj: Any, key: str) -> Any:
    """兼容 pydantic 模型与 dict 的字段读取."""
    if isinstance(obj, dict):
        return obj.get(key)
    if hasattr(obj, key):
        return getattr(obj, key)
    return None


def citation_index(state_values: dict) -> dict[str, dict]:
    """标签 → 引用资料(标注/类型/正文), 用于补齐旧事件的纯标题引用."""
    idx: dict[str, dict] = {}
    record = state_values.get("prompts_record") or {}
    laws = list(state_values.get("law_results") or [])
    laws += list(_field(record, "laws_results") or [])
    for law in laws:
        label = f"{_field(law, 'law_title') or ''} {_field(law, 'article_number') or ''}".strip()
        if label and label not in idx:
            idx[label] = {
                "label": label,
                "type": "law",
                "content": str(_field(law, "content") or "").strip(),
            }
    for doc in state_values.get("rag_documents") or []:
        label = (_field(doc, "case_number") or _field(doc, "case_cause") or "").strip()
        if label:
            idx[label] = {
                "label": label,
                "type": "case",
                "content": str(_field(doc, "chunk_text") or "").strip(),
            }
    return idx


def enrich_citations(turns: list[dict], state_values: dict) -> list[dict]:
    """旧事件引用是标题字符串, 按 state 索引补成 {label,type,content}."""
    idx = citation_index(state_values)
    for turn in turns:
        cites = []
        for c in turn.get("citations") or []:
            if isinstance(c, str):
                cites.append(idx.get(c, c))
            else:
                cites.append(c)
        turn["citations"] = cites
    return turns


def build_transcript(
    session_id: str,
    state_values: dict,
    events: list[dict],
    runs: list[dict],
) -> dict:
    """transcript 主体(turns/final); elements/对话日志由调用方补齐."""
    turns = build_turns(list(state_values.get("messages") or []))
    attach_dialogue(turns, events)
    enrich_citations(turns, state_values)
    attach_runs(turns, runs)
    final = None
    for turn in reversed(turns):
        if turn.get("answer"):
            final = {
                "answer": turn["answer"],
                "citations": turn.get("citations") or [],
                "ts": turn.get("ts"),
            }
            break
    return {"session_id": session_id, "turns": turns, "final": final}


async def fetch_session_runs(session_id: str) -> list[dict]:
    """trace_runs 读会话内全部 run; 失败返回 [](导出/转录降级)."""
    from lawApp_LangGraph import db

    try:
        pool = await db.get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT run_id, run_type, status, query, final_answer, "
                "started_at, ended_at FROM trace_runs "
                "WHERE session_id = %s ORDER BY started_at",
                (session_id,),
            )
            rows = await cur.fetchall()
        return [
            {
                "run_id": r[0],
                "run_type": r[1],
                "status": r[2],
                "query": r[3] or "",
                "final_answer": r[4] or "",
                "started_at": r[5],
                "ended_at": r[6],
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning("会话 run 读取失败(降级空): %s", e)
        return []


def export_dir_for(session_id: str) -> Path:
    """exports/<安全 sid>/; sid 非白名单字符替换为 _ 防路径穿越."""
    safe = _SAFE_ID.sub("_", session_id or "session")[:120]
    return Path(settings.export_dir) / safe


def write_export(payload: dict) -> dict:
    """原子写本地 JSON(同目录 tmp + os.replace), 返回路径/大小/sha256."""
    directory = export_dir_for(str(payload.get("session_id") or "session"))
    directory.mkdir(parents=True, exist_ok=True)
    name = f"{datetime.now():%Y%m%d-%H%M%S-%f}.json"
    target = directory / name
    tmp = directory / f"{name}.tmp"
    body = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        **payload,
    }
    text = json.dumps(body, ensure_ascii=False, indent=2, default=str)
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)
    data = target.read_bytes()
    return {
        "file": target.as_posix(),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
