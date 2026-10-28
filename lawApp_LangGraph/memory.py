"""短期记忆 — 结构化增量压缩 / 最近轮次渲染 / HITL 回答保留。

设计: docs/superpowers/specs/2026-10-23-minor-states-monitoring-design.md §八
- 惰性触发: 未压缩轮数 ≥ memory_compress_after_turns;
- flash 严格 schema, 失败保留旧 summary 与 recent 原文;
- 不可压缩内容不经过本模块改写, 仍由 state 原字段注入 context。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from lawApp_LangGraph.config import settings

logger = logging.getLogger("lawApp.memory")


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _as_dict(obj: Any) -> dict:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            return {}
    return {}


def _msg_role(m: Any) -> Optional[str]:
    text = getattr(m, "type", None)
    if text == "human":
        return "user"
    if text == "ai":
        return "assistant"
    # 兼容 dict 形态
    if isinstance(m, dict):
        role = m.get("role") or m.get("type")
        return {"human": "user", "ai": "assistant"}.get(role, role)
    return None


def _msg_text(m: Any) -> str:
    content = getattr(m, "content", None)
    if content is None and isinstance(m, dict):
        content = m.get("content")
    return str(content or "")


def _clean_messages(messages: list) -> list[dict]:
    out = []
    for m in messages:
        role = _msg_role(m)
        if role not in ("user", "assistant"):
            continue
        text = _msg_text(m).strip()
        if not text:
            continue
        out.append({"role": role, "text": text})
    return out


def _clip(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - 12)] + "…[截断]"


def _total_chars(items: list[dict]) -> int:
    return sum(len(x.get("text", "")) for x in items)


def render_recent(state: Any) -> str:
    """最近 N 轮渲染; 有 memory.recent 优先, 否则回退 messages。"""
    mem = _as_dict(_get(state, "conversation_memory"))
    recent = mem.get("recent") or []
    items = []
    if recent:
        for x in recent:
            d = _as_dict(x)
            if d.get("role") in ("user", "assistant") and d.get("text"):
                items.append(
                    {
                        "role": d["role"],
                        "text": _clip(str(d["text"]), settings.memory_per_msg_limit),
                    }
                )
    else:
        msgs = _clean_messages(list(_get(state, "messages") or []))
        turns = settings.memory_recent_turns
        # 一轮 = user + assistant; 取最后 2*turns 条 user/assistant
        items = [
            {
                "role": x["role"],
                "text": _clip(x["text"], settings.memory_per_msg_limit),
            }
            for x in msgs[-turns * 2 :]
        ]

    # 剔除尾部与当前 query 相同的 user 条(写侧已 append 了本轮 query)
    query = str(_get(state, "query") or "").strip()
    if items and items[-1]["role"] == "user" and items[-1]["text"].strip() == query:
        items.pop()

    # 总量上限: 从尾部保留
    kept: list[dict] = []
    used = 0
    for item in reversed(items):
        size = len(item["text"]) + len(item["role"]) + 4
        if used + size > settings.memory_total_limit:
            break
        kept.append(item)
        used += size
    kept.reverse()
    if not kept:
        return ""
    lines = [
        f"{'用户' if x['role'] == 'user' else '助手'}: {x['text']}" for x in kept
    ]
    return "[最近对话]\n" + "\n".join(lines)


def memory_digest(state: Any) -> str:
    """只含 facts + summary(供 element_assess, 不带 recent 原文)。"""
    mem = _as_dict(_get(state, "conversation_memory"))
    if not mem:
        return ""
    facts = _as_dict(mem.get("facts"))
    summary = _as_dict(mem.get("summary"))
    parts = []
    if summary:
        for key in ("conversation_goal", "progress", "open_items", "decisions"):
            value = summary.get(key)
            if value:
                parts.append(f"{key}: {value}")
    if facts:
        parts.append(f"facts: {json.dumps(facts, ensure_ascii=False)}")
    if not parts:
        return ""
    return "[历史记忆]\n" + "\n".join(parts)


#  压缩 schema


class SummaryUpdate(BaseModel):
    conversation_goal: str = ""
    progress: list[str] = Field(default_factory=list)
    open_items: list[str] = Field(default_factory=list)
    prior_retrieval: list[dict] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)


class CompressionResult(BaseModel):
    summary_update: SummaryUpdate = Field(default_factory=SummaryUpdate)
    facts_patch: dict = Field(default_factory=dict)
    retrieval_digest: list[dict] = Field(default_factory=list)
    dropped: list[str] = Field(default_factory=list)


_COMPRESS_PROMPT = """你是法律咨询会话的结构化记忆压缩器.把旧对话压缩成字段化摘要.

## 现有摘要
{summary}

## 已知事实
{facts}

## 案件要素(不可改写, 仅用于理解)
{elements}

## 旧对话消息
{messages}

## HITL 问答(原文, 不可丢)
{hitl}

## 输出要求
只输出一个 JSON 对象, 字段必须如下:
{{"summary_update": {{"conversation_goal": "...", "progress": ["..."], "open_items": ["..."], "prior_retrieval": [{{"topic": "...", "conclusion": "...", "case_numbers": ["..."]}}], "decisions": ["..."]}}, "facts_patch": {{"key": "value"}}, "retrieval_digest": [{{"topic": "...", "conclusion": "...", "case_numbers": ["..."]}}], "dropped": ["..."]}}
规则:
- 不编造; 只保留有依据的信息
- facts_patch 只增/改, 不删除已确认事实
- progress/open_items/decisions 各不超过 5 条
- HITL 问答必须保留在 progress/open_items 中
"""


def _extract_json(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
        if t.endswith("```"):
            t = t[:-3]
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no json object")
    return json.loads(t[start : end + 1])


async def _compress_once(state: Any) -> dict:
    from langchain_core.messages import SystemMessage

    from lawApp_LangGraph.LangGraph_lawApp import get_executor_llm

    mem = _as_dict(_get(state, "conversation_memory"))
    msgs = _clean_messages(list(_get(state, "messages") or []))
    drop_count = settings.memory_recent_turns * 2
    old_msgs = msgs[:-drop_count] if drop_count and len(msgs) > drop_count else []
    hitl = [
        {"question": _as_dict(h).get("question"), "answer": _as_dict(h).get("answer")}
        for h in (_get(state, "clarify_history") or [])
        if _as_dict(h).get("answer")
    ]
    prompt = _COMPRESS_PROMPT.format(
        summary=json.dumps(mem.get("summary") or {}, ensure_ascii=False),
        facts=json.dumps(mem.get("facts") or {}, ensure_ascii=False),
        elements=json.dumps(
            _as_dict(_get(state, "case_elements")), ensure_ascii=False
        ),
        messages=json.dumps(old_msgs[-40:], ensure_ascii=False),
        hitl=json.dumps(hitl, ensure_ascii=False),
    )
    llm = get_executor_llm()
    resp = await asyncio.wait_for(
        llm.ainvoke([SystemMessage(content=prompt)]),
        timeout=settings.memory_compress_timeout_s,
    )
    raw = getattr(resp, "content", "") or ""
    return CompressionResult.model_validate(_extract_json(str(raw))).model_dump()


def _merge(mem: dict, result: dict, total_turns: int) -> dict:
    old = dict(mem or {})
    summary = dict(_as_dict(old.get("summary")))
    upd = result.get("summary_update") or {}
    for key in ("conversation_goal", "progress", "open_items", "decisions"):
        value = upd.get(key)
        if value:
            summary[key] = value[:5] if isinstance(value, list) else value
    if upd.get("prior_retrieval"):
        summary["prior_retrieval"] = upd["prior_retrieval"][:5]
    facts = dict(_as_dict(old.get("facts")))
    for k, v in (result.get("facts_patch") or {}).items():
        if v not in (None, ""):
            facts[k] = v
    digest = result.get("retrieval_digest") or []
    if digest:
        old_digest = list(old.get("retrieval_digest") or [])
        old["retrieval_digest"] = (old_digest + digest)[-settings.memory_digest_limit :]
    old.update(
        {
            "version": 2,
            "summary": summary,
            "facts": facts,
            "compressed_until_turn": total_turns,
            "updated_at": datetime.now().isoformat(),
        }
    )
    return old


async def maybe_compress(state: Any) -> dict:
    """惰性压缩; 不满足条件/失败时原样返回旧 memory(不改半成品)。"""
    mem = _as_dict(_get(state, "conversation_memory"))
    if not settings.memory_compress_enabled:
        return mem
    msgs = _clean_messages(list(_get(state, "messages") or []))
    total_turns = sum(1 for m in msgs if m["role"] == "user")
    compressed = int(mem.get("compressed_until_turn") or 0)
    if total_turns - compressed < settings.memory_compress_after_turns:
        return mem
    if total_turns < settings.memory_compress_after_turns:
        return mem
    try:
        result = await _compress_once(state)
        merged = _merge(mem, result, total_turns)
        merged["recent"] = [
            {
                "role": m["role"],
                "text": _clip(m["text"], settings.memory_per_msg_limit),
                "ts": datetime.now().isoformat(),
            }
            for m in msgs[-settings.memory_recent_turns * 2 :]
        ]
        return merged
    except Exception as e:
        logger.warning("记忆压缩失败, 保留旧摘要(旁路): %s", e)
        return mem


async def digest_previous_retrieval(state: Any) -> dict:
    """ingest 清空原文前把本轮 top-3 结论写入 retrieval_digest(跨轮用)。

    不调 LLM: 用案号 + 案由 + chunk 首句, 稳定且零额外成本; 失败返回旧 memory。
    """
    mem = _as_dict(_get(state, "conversation_memory"))
    docs = list(_get(state, "rag_documents") or [])
    if not docs:
        return mem
    items = []
    for d in docs[: settings.memory_digest_limit]:
        x = _as_dict(d)
        text = str(x.get("chunk_text") or "").strip()
        conclusion = text.split("。")[0][:160] if text else ""
        items.append(
            {
                "topic": x.get("case_cause") or "",
                "conclusion": conclusion,
                "case_numbers": [x.get("case_number")] if x.get("case_number") else [],
            }
        )
    if not items:
        return mem
    mem = dict(mem)
    old = list(mem.get("retrieval_digest") or [])
    mem["retrieval_digest"] = (old + items)[-settings.memory_digest_limit :]
    mem["updated_at"] = datetime.now().isoformat()
    return mem


def hitl_message_update(state: Any, answer: str) -> dict:
    """HITL 回答写入 messages(带 [补充] 前缀, 去重)。"""
    text = f"[补充] {str(answer).strip()}"
    if not str(answer).strip():
        return {}
    from langchain_core.messages import HumanMessage

    for m in reversed(list(_get(state, "messages") or [])):
        if getattr(m, "type", None) == "human" and _msg_text(m).strip() == text:
            return {}
        if getattr(m, "type", None) == "ai":
            break
    return {"messages": [HumanMessage(content=text)]}
