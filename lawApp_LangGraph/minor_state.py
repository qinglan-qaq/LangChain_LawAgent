"""minorStates 内容投影 + 单事务监控写入器。

设计: docs/superpowers/specs/2026-10-23-minor-states-monitoring-design.md §二/§四
- 投影只做结构裁剪, 绝不落 messages / prompts 全文 / 二进制;
- 生命周期字段(stage_chain)与内容字段(minor_states)由 MonitorWriter 单事务写;
- 观测旁路: 投影异常与写入失败均不阻塞业务。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Optional

from lawApp_LangGraph.config import settings

logger = logging.getLogger("lawApp.minor")

#  投影白/黑名单

_BLACK_KEYS = {
    "messages",
    "prompts_record",
    "final_prompts",
    "state",
    "tool_calls",
    "reasoning",
    "raw",
}
_BAD_VALUE = re.compile(r"^\s*(data:|<html|<style|<script)", re.IGNORECASE)
_MAX_TEXT_ITEM = 400


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


def _clip(value: str, limit: int) -> tuple[str, int]:
    """截断并返回 (文本, 被截字数); 截断标记计入返回文本。"""
    if value is None:
        return "", 0
    text = str(value)
    if limit <= 0 or len(text) <= limit:
        return text, 0
    keep = max(0, limit - 24)
    dropped = len(text) - keep
    return f"{text[:keep]}…[截断 {dropped} 字]", dropped


def _clean_value(value: Any, depth: int = 0) -> Any:
    """递归清洗: 黑名单键丢弃, HTML/data URI 丢弃, 长文本截 400。"""
    if depth > 6:
        return None
    if isinstance(value, str):
        if _BAD_VALUE.search(value):
            return None
        if value.startswith("<!DOCTYPE") or value.startswith("<?xml"):
            return None
        return value[:_MAX_TEXT_ITEM]
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if str(k) in _BLACK_KEYS:
                continue
            cv = _clean_value(v, depth + 1)
            if cv not in (None, "", [], {}):
                out[str(k)] = cv
        return out
    if isinstance(value, (list, tuple)):
        items = [_clean_value(v, depth + 1) for v in value]
        return [v for v in items if v not in (None, "", [], {})][:20]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:_MAX_TEXT_ITEM]


def _doc_preview(docs: Any, kind: str = "rag") -> list[dict]:
    if not isinstance(docs, (list, tuple)):
        return []
    limit = max(1, settings.minor_doc_preview_count)
    out = []
    for d in docs[:limit]:
        item = _as_dict(d)
        if kind == "rag":
            out.append(
                {
                    "case_number": item.get("case_number", ""),
                    "case_cause": item.get("case_cause", ""),
                    "relevance_score": item.get("relevance_score")
                    or item.get("hybrid_score"),
                    "rrf_score": item.get("rrf_score"),
                    "text": str(item.get("chunk_text") or "")[:200],
                }
            )
        elif kind == "law":
            out.append(
                {
                    "law_title": item.get("law_title", ""),
                    "article_number": item.get("article_number", ""),
                    "content": str(item.get("content") or "")[:200],
                }
            )
        else:
            out.append(
                {
                    "title": item.get("title", ""),
                    "link": item.get("link", ""),
                    "summary": str(item.get("snippet") or item.get("summary") or "")[:200],
                }
            )
    return out


def _elements_map(state: Any) -> dict:
    ce = _as_dict(_get(state, "case_elements"))
    out = {}
    for e in ce.get("elements") or []:
        d = _as_dict(e)
        if d.get("status") == "known" and d.get("value"):
            out[d.get("key", "")] = d.get("value")
    return out


def _pending_elements(state: Any) -> list[str]:
    ce = _as_dict(_get(state, "case_elements"))
    return [
        d.get("key")
        for d in (_as_dict(e) for e in (ce.get("elements") or []))
        if d.get("status") == "missing"
    ]


def _plan_lines(plan: Any) -> list[str]:
    lines = []
    for s in plan or []:
        d = _as_dict(s)
        lines.append(
            f"{d.get('step_id', '?')}. {d.get('tool_name') or '无工具'} — "
            f"{str(d.get('description') or '')[:120]}"
        )
    return lines


def _rag_digest(state: Any, limit: int = 3) -> dict:
    docs = _get(state, "rag_documents") or []
    ev = _as_dict(_get(state, "evaluation"))
    return {
        "rag_cases": _doc_preview(docs, "rag")[:limit],
        "rag_count": len(docs),
        "verdict": ev.get("quality_verdict") or "",
    }


def _laws_digest(state: Any, limit: int = 3) -> dict:
    laws = _get(state, "law_results") or []
    return {"laws": _doc_preview(laws, "law")[:limit], "law_count": len(laws)}


def _web_digest(state: Any, limit: int = 3) -> dict:
    web = _get(state, "web_search_results") or []
    return {"web_results": _doc_preview(web, "web")[:limit], "web_count": len(web)}


#  input 投影: 每节点只取需要的人读信息


def _input_parts(node_name: str, state: Any) -> tuple[str, dict]:
    q = str(_get(state, "query") or "")
    mode = _get(state, "mode") or "attorney"
    meta: dict[str, Any] = {"query": len(q)}

    if node_name == "ingest":
        mem = _as_dict(_get(state, "conversation_memory"))
        text = (
            f"问题: {q[:500]}\n模式: {mode} | 文书类型: {_get(state, 'doc_type') or '-'}\n"
            f"记忆: {'命中' if mem.get('summary') or mem.get('facts') else '无'}"
        )
        return text, {**meta, "mode": mode}

    if node_name == "risk_gate":
        return f"问题: {q[:800]}\n模式: {mode}", meta

    if node_name == "element_assess":
        ce = _as_dict(_get(state, "case_elements"))
        hist = _get(state, "clarify_history") or []
        last = _as_dict(hist[-1]) if hist else {}
        digest = ""
        try:
            digest = _get(state, "case_elements").digest()
        except Exception:
            digest = " | ".join(f"{k}:{v}" for k, v in _elements_map(state).items())
        text = (
            f"问题: {q[:800]}\n要素: {digest}\n"
            f"上轮问题: {last.get('question', '') or '(无)'}\n"
            f"上轮回答: {last.get('answer', '') or '(无)'}\n"
            f"轮次: {_get(state, 'clarify_rounds', 0)}"
        )
        return text, {**meta, "case_elements": len(digest), "history": len(hist)}

    if node_name == "ask_element":
        qs = _get(state, "pending_questions") or []
        first = _as_dict(qs[0]) if qs else {}
        opts = first.get("options") or []
        text = (
            f"第 {_get(state, 'clarify_rounds', 0) + 1} 轮反问\n"
            f"问题: {first.get('question', '')}\n选项: {' / '.join(map(str, opts))}"
        )
        return text, {**meta, "round": _get(state, "clarify_rounds", 0) + 1}

    if node_name == "planner":
        steps = _plan_lines(_get(state, "plan"))
        rag = _rag_digest(state)
        text = (
            f"问题: {q[:800]}\n要素: {_elements_map(state)}\n"
            f"已有案例: {rag['rag_count']} 条, 评估: {rag['verdict'] or '未评估'}\n"
            f"上级计划: {'; '.join(steps) if steps else '(无)'}"
        )
        return text, {**meta, "rag_cases": rag["rag_count"]}

    if node_name == "executor":
        idx = _get(state, "current_step_index", 0) or 0
        plan = _get(state, "plan") or []
        step = _as_dict(plan[idx]) if idx < len(plan) else {}
        rag = _rag_digest(state)
        laws = _laws_digest(state)
        web = _web_digest(state)
        text = (
            f"步骤 {idx + 1}/{len(plan)}: {step.get('description', '')}\n"
            f"工具: {step.get('tool_name') or '无'}\n要素: {_elements_map(state)}\n"
            f"案例: {rag['rag_count']} 条 | verdict={rag['verdict'] or '-'}\n"
            f"法条: {laws['law_count']} 条 | 网络: {web['web_count']} 条"
        )
        return text, {**meta, "rag_cases": rag["rag_count"], "laws": laws["law_count"]}

    if node_name == "tools":
        idx = _get(state, "current_step_index", 0) or 0
        plan = _get(state, "plan") or []
        step = _as_dict(plan[idx]) if idx < len(plan) else {}
        return (
            f"工具: {step.get('tool_name') or '-'}\n参数: "
            f"{json.dumps(_as_dict(step.get('args')), ensure_ascii=False)[:500]}",
            meta,
        )

    if node_name == "merge":
        msgs = _get(state, "messages") or []
        last = msgs[-1] if msgs else None
        content = str(_get(last, "content") or "")[:300]
        return f"工具结果: {content}", {**meta, "tool_result": len(content)}

    if node_name == "replan_check":
        rag = _rag_digest(state)
        laws = _laws_digest(state)
        web = _web_digest(state)
        text = (
            f"案例: {rag['rag_count']} 条 | verdict={rag['verdict'] or '-'}\n"
            f"法条: {laws['law_count']} 条 | 网络: {web['web_count']} 条\n"
            f"错误: {_get(state, 'error') or '(无)'}"
        )
        return text, {**meta, "rag_cases": rag["rag_count"]}

    if node_name == "mid_clarify":
        docs = _get(state, "rag_documents") or []
        top = "\n".join(
            f"- [{_as_dict(d).get('case_number', '')}] "
            f"{str(_as_dict(d).get('chunk_text') or '')[:120]}"
            for d in docs[:5]
        )
        return f"问题: {q[:800]}\n检索摘要:\n{top or '(空)'}", meta

    if node_name == "hitl_degrade":
        return (
            f"失败工具: {_get(state, 'error') or '-'}\n"
            f"连续失败: {_get(state, 'error_streak', 0)}",
            meta,
        )

    if node_name == "hitl_budget":
        return (
            f"缺失: {' / '.join(_pending_elements(state)) or '-'}\n"
            f"已用工具调用: {len(_get(state, 'tool_calls') or [])}/{settings.max_rounds}",
            meta,
        )

    if node_name == "replanner":
        rag = _rag_digest(state)
        return (
            f"已执行: {len(_get(state, 'tool_calls') or [])} 次工具\n"
            f"案例: {rag['rag_count']} 条 | 原因: {_get(state, 'insufficient_reason') or '-'}\n"
            f"可用工具: {len(_get(state, 'plan') or [])} 步计划",
            meta,
        )

    if node_name == "element_fill":
        return (
            f"待补要素: {', '.join(_pending_elements(state)) or '-'}\n"
            f"轮次: {_get(state, 'element_fill_rounds', 0)}",
            meta,
        )

    if node_name in ("chitchat", "finalize"):
        return (
            f"模式: {mode} | 问题: {q[:800]}\n"
            f"案例: {len(_get(state, 'rag_documents') or [])} 条 | "
            f"法条: {len(_get(state, 'law_results') or [])} 条",
            meta,
        )

    return f"节点: {node_name}\n问题: {q[:500]}", meta


def project_input(
    node_name: str, state: Any, context_meta: Optional[dict] = None
) -> tuple[str, dict]:
    """节点输入投影; 任何异常降级为占位文本, 不阻塞节点。"""
    try:
        text, meta = _input_parts(node_name, state)
        if context_meta:
            meta = {**context_meta, **meta}
        clipped, dropped = _clip(text, settings.minor_input_text_limit)
        if dropped:
            meta["truncated_chars"] = {"input": dropped}
        return clipped, _clean_value(meta) or {}
    except Exception as e:
        logger.warning("minor 输入投影失败(旁路): %s", e)
        return (
            "输入投影失败",
            {"projection_error": repr(e)[:300], "truncated_chars": {}},
        )


#  output 投影


def _output_parts(node_name: str, ret: Any, state: Any) -> tuple[str, dict]:
    d = _as_dict(ret)
    q = str(_get(state, "query") or "")
    out: dict[str, Any] = {}

    if node_name == "ingest":
        ce = _as_dict(d.get("case_elements") or _get(state, "case_elements"))
        keys = [
            _as_dict(e).get("key") for e in (ce.get("elements") or [])
        ]
        out = {
            "mode": d.get("mode") or _get(state, "mode"),
            "doc_type": d.get("doc_type") or _get(state, "doc_type"),
            "element_keys": keys,
            "element_count": len(keys),
            "memory_restored": bool(_get(state, "conversation_memory")),
        }
        return (
            f"初始化完成: {out['mode']}/{out['doc_type'] or '-'}, "
            f"要素 {out['element_count']} 个",
            out,
        )

    if node_name == "risk_gate":
        hitl = _as_dict(d.get("hitl_event"))
        out = {
            "level": "high" if hitl or d.get("final_answer") else "normal",
            "category": "risk",
            "reason": str(d.get("final_answer") or "")[:200],
            "hitl_type": hitl.get("type"),
        }
        return ("高风险中止" if d.get("final_answer") else "无高风险"), out

    if node_name == "element_assess":
        ce = _as_dict(d.get("case_elements"))
        updates = {
            _as_dict(e).get("key"): _as_dict(e).get("value")
            for e in (ce.get("elements") or [])
            if _as_dict(e).get("updated_by") == "assess"
        }
        qs = [_as_dict(x) for x in (d.get("pending_questions") or [])]
        out = {
            "question_category": d.get("question_category"),
            "applicable": d.get("question_category") not in ("chitchat", "other"),
            "updates": updates,
            "na_keys": [
                _as_dict(e).get("key")
                for e in (ce.get("elements") or [])
                if _as_dict(e).get("status") == "na"
            ],
            "promote_keys": [],
            "pending_questions": [x.get("question") for x in qs],
        }
        text = "\n".join(str(x.get("question") or "") for x in qs) or "(无需反问)"
        return text, out

    if node_name in ("ask_element", "mid_clarify", "element_fill"):
        ce = _as_dict(d.get("case_elements") or _get(state, "case_elements"))
        qs = _get(state, "pending_questions") or []
        first = _as_dict(qs[0]) if qs else {}
        answer = ""
        hist = d.get("clarify_history")
        if isinstance(hist, list) and hist:
            answer = _as_dict(hist[-1]).get("answer", "")
        out = {
            "round": _get(state, "clarify_rounds", 0),
            "question": first.get("question", ""),
            "options": first.get("options") or [],
            "answer": answer,
            "skipped": not bool(answer),
            "element_keys": [first.get("key")] if first.get("key") else [],
        }
        return (f"用户回答: {answer}" if answer else "等待用户回答"), out

    if node_name == "chitchat":
        answer = str(d.get("final_answer") or "")
        return answer, {"answer_len": len(answer), "history_used": True}

    if node_name == "planner":
        steps = [
            {
                "id": _as_dict(s).get("step_id"),
                "tool": _as_dict(s).get("tool_name"),
                "description": _as_dict(s).get("description"),
                "args": _as_dict(s).get("args") or {},
            }
            for s in (d.get("plan") or _get(state, "plan") or [])
        ]
        out = {
            "steps": steps,
            "step_count": len(steps),
            "includes_element_fill": any(
                s.get("tool") == "request_element_fill" for s in steps
            ),
        }
        return "\n".join(_plan_lines(d.get("plan") or [])), out

    if node_name == "executor":
        plan = d.get("plan") or _get(state, "plan") or []
        idx = _get(state, "current_step_index", 0) or 0
        step = _as_dict(plan[idx]) if idx < len(plan) else {}
        tool = step.get("tool_name")
        action = "advance"
        tool_calls = []
        for m in d.get("messages") or []:
            for tc in (_get(m, "tool_calls") or []):
                tool_calls.append(tc)
        if tool_calls:
            action = "tool_call"
            tool = tool_calls[0].get("name") or tool
        if d.get("doc_fields"):
            action = "docx_fields"
        out = {
            "step_index": idx,
            "action": action,
            "tool": tool,
            "args": (tool_calls[0].get("args") if tool_calls else {}) or {},
            "hitl": action if d.get("hitl_event") else None,
            "retry": 0,
            "extracted_fields": list((d.get("doc_fields") or {}).keys()),
        }
        text = (
            f"调用 {out['tool']}({json.dumps(out['args'], ensure_ascii=False)[:300]})"
            if action == "tool_call"
            else "推进步骤"
        )
        return text, out

    if node_name == "tools":
        msgs = d.get("messages") or []
        content = str(_get(msgs[-1], "content") or "") if msgs else ""
        try:
            parsed = json.loads(content) if content else {}
        except Exception:
            parsed = {}
        docs = parsed.get("rag_documents") or []
        laws = parsed.get("law_results") or []
        web = parsed.get("web_search_results") or []
        ev = _as_dict(parsed.get("evaluation"))
        out = {
            "tool": _get(msgs[-1], "name") if msgs else None,
            "ok": _get(msgs[-1], "status") != "error" if msgs else False,
            "doc_count": len(docs),
            "law_count": len(laws),
            "web_count": len(web),
            "verdict": ev.get("quality_verdict"),
            "path": parsed.get("pdf_path") or parsed.get("docx_path"),
            "error_code": None,
        }
        titles = [
            _as_dict(x).get("case_number") or _as_dict(x).get("title")
            for x in (docs[:3] or laws[:3] or web[:3])
        ]
        return (
            f"结果: 案例 {len(docs)} / 法条 {len(laws)} / 网络 {len(web)}"
            + (f" | {', '.join(str(t) for t in titles if t)}" if titles else ""),
            out,
        )

    if node_name == "merge":
        out = {
            "updated_keys": [k for k in d if k not in _BLACK_KEYS],
            "step_index": d.get("current_step_index"),
            "tool_calls_count": len(d.get("tool_calls") or []),
            "doc_count": len(_get(state, "rag_documents") or []),
            "eval_verdict": _as_dict(
                d.get("evaluation") or _get(state, "evaluation")
            ).get("quality_verdict"),
        }
        return (
            f"合并结果: 更新 {len(out['updated_keys'])} 键, "
            f"推进第 {(out['step_index'] or 0) + 1} 步",
            out,
        )

    if node_name == "replan_check":
        out = {
            "quality_verdict": _as_dict(_get(state, "evaluation")).get("quality_verdict"),
            "replan_needed": d.get("replan_needed"),
            "insufficient_reason": d.get("insufficient_reason"),
        }
        return str(d.get("insufficient_reason") or "none"), out

    if node_name == "hitl_degrade":
        choice = ""
        for m in d.get("messages") or []:
            choice = str(_get(m, "content") or "")[:200]
        out = {
            "failed_tool": _get(state, "error"),
            "error_streak": _get(state, "error_streak", 0),
            "choice": choice,
        }
        return (f"用户选择: {choice}" if choice else "等待用户选择"), out

    if node_name == "hitl_budget":
        return "等待用户选择", {
            "missing": _pending_elements(state),
            "choice": str(d.get("hitl_event") or ""),
        }

    if node_name == "replanner":
        plan = d.get("plan") or []
        steps = [_as_dict(s) for s in plan]
        out = {
            "steps": [
                {"id": s.get("step_id"), "tool": s.get("tool_name")} for s in steps
            ],
            "next_id": (steps[-1].get("step_id") if steps else None),
            "empty": not steps,
        }
        return "\n".join(_plan_lines(plan)) or "(无补充计划)", out

    if node_name in ("finalize", "chitchat"):
        answer = str(d.get("final_answer") or "")
        out = {
            "branch": "reuse" if answer and not d.get("final_prompts") else "generate",
            "reused_answer": bool(d.get("final_answer")),
            "answer_len": len(answer),
            "citations_count": len(_get(state, "rag_documents") or []),
            "laws_count": len(_get(state, "law_results") or []),
            "cases_count": len(_get(state, "rag_documents") or []),
            "context_chars": len(str(d.get("final_prompts") or "")),
        }
        return answer, out

    clean = _clean_value(d if isinstance(d, dict) else ret)
    return (
        str(clean)[:500] if clean not in (None, {}, []) else "无输出",
        _clean_value(clean) or {},
    )


def _shrink_json(value: Any, max_bytes: int) -> tuple[Any, bool]:
    """超限收缩: 列表截断 → 长文本截断 → 丢最大键。"""
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    if len(encoded.encode("utf-8")) <= max_bytes:
        return value, False
    if isinstance(value, dict):
        value = dict(value)
        for k in sorted(value, key=lambda x: len(str(value[x])), reverse=True):
            v = value[k]
            if isinstance(v, list) and v:
                value[k] = v[: max(1, len(v) // 2)]
            elif isinstance(v, str) and len(v) > 200:
                value[k] = v[:200]
            else:
                value.pop(k, None)
            encoded = json.dumps(value, ensure_ascii=False, default=str)
            if len(encoded.encode("utf-8")) <= max_bytes:
                return value, True
        return {}, True
    if isinstance(value, list):
        return value[:1], True
    return str(value)[:500], True


def project_output(
    node_name: str, ret: Any, state: Any
) -> tuple[str, dict]:
    """节点输出投影; 任何异常降级为 projection_error, 不抛。"""
    try:
        text, out = _output_parts(node_name, ret, state)
        text = "" if text is None else str(text)
        if _BAD_VALUE.search(text):
            text = ""
        clipped, dropped = _clip(text, settings.minor_output_text_limit)
        out = _clean_value(out) or {}
        out, shrunk = _shrink_json(out, settings.minor_json_max_bytes)
        if dropped:
            out.setdefault("_truncated_chars", {})["output"] = dropped
        if shrunk:
            out["_json_truncated"] = True
        return clipped, out
    except Exception as e:
        logger.warning("minor 输出投影失败(旁路): %s", e)
        return "", {"projection_error": repr(e)[:300]}


#  MonitorWriter: 单队列 / 单消费者 / 单事务


def _stage_enabled() -> bool:
    return os.getenv("STAGE_ZIPPER", "1") != "0"


def _minor_enabled() -> bool:
    if not _stage_enabled():
        return False
    if os.getenv("MINOR_STATES", "1") == "0":
        return False
    return bool(settings.minor_states_enabled)


_QUEUE: Optional[asyncio.Queue] = None
_CONSUMER: Optional[asyncio.Task] = None
_LOOP: Optional[asyncio.AbstractEventLoop] = None
_STAGE_IDS: dict[tuple[str, str, int], int] = {}
_ROUTES: dict[tuple[str, str, int], tuple[str, str]] = {}


def _loop_changed() -> bool:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    global _LOOP
    if _LOOP is not loop:
        _LOOP = loop
        return True
    return False


def _ensure_consumer() -> Optional[asyncio.Queue]:
    global _QUEUE, _CONSUMER
    if _loop_changed() and _QUEUE is not None:
        # 旧 loop 已结束: 丢弃旧队列引用, 新 loop 重建
        _QUEUE = None
        _CONSUMER = None
        _STAGE_IDS.clear()
        _ROUTES.clear()
    if _QUEUE is None:
        _QUEUE = asyncio.Queue(maxsize=max(1, settings.minor_writer_queue_max))
    if _CONSUMER is None or _CONSUMER.done():
        try:
            _CONSUMER = asyncio.get_running_loop().create_task(_consume())
        except RuntimeError:
            return None
    return _QUEUE


def enqueue(op: dict) -> None:
    """非阻塞入队; 满则丢弃(观测旁路)。"""
    try:
        q = _ensure_consumer()
        if q is None:
            return
        q.put_nowait(op)
    except asyncio.QueueFull:
        logger.warning("minor writer 队列已满, 丢弃 op=%s", op.get("op"))
    except RuntimeError:
        pass


def enqueue_open(
    run_id: str,
    session_id: str,
    node_name: str,
    seq: int,
    *,
    attempt: int = 1,
    input_text: str = "",
    context_meta: Optional[dict] = None,
    parent_run_id: Optional[str] = None,
) -> None:
    if not _stage_enabled():
        return
    enqueue(
        {
            "op": "open",
            "run_id": run_id,
            "session_id": session_id,
            "node_name": node_name,
            "seq": seq,
            "attempt": attempt,
            "input_text": input_text,
            "context_meta": context_meta or {},
            "parent_run_id": parent_run_id,
            "minor": _minor_enabled(),
        }
    )


def enqueue_close(
    run_id: str,
    node_name: str,
    seq: int,
    *,
    status: str,
    latency_ms: int,
    phase: str = "done",
    output_text: str = "",
    output_json: Optional[dict] = None,
    context_meta: Optional[dict] = None,
    metrics: Optional[dict] = None,
    error: Optional[str] = None,
    refs: Optional[dict] = None,
) -> None:
    if not _stage_enabled():
        return
    enqueue(
        {
            "op": "close",
            "run_id": run_id,
            "node_name": node_name,
            "seq": seq,
            "status": status,
            "latency_ms": latency_ms,
            "phase": phase,
            "output_text": output_text,
            "output_json": output_json or {},
            "context_meta": context_meta or {},
            "metrics": metrics or {},
            "error": error,
            "refs": refs or {},
            "minor": _minor_enabled(),
        }
    )


def enqueue_route(
    run_id: str, node_name: str, seq: int, route: str, reason: str = ""
) -> None:
    if not _minor_enabled():
        return
    enqueue(
        {
            "op": "route",
            "run_id": run_id,
            "node_name": node_name,
            "seq": seq,
            "route": route,
            "route_reason": reason,
        }
    )


async def _consume() -> None:
    while True:
        q = _QUEUE
        if q is None:
            return
        try:
            first = await q.get()
        except asyncio.CancelledError:
            raise
        batch = [first]
        deadline = time.monotonic() + settings.minor_writer_flush_ms / 1000
        while len(batch) < max(1, settings.minor_writer_batch):
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                break
            try:
                batch.append(await asyncio.wait_for(q.get(), timeout))
            except asyncio.TimeoutError:
                break
        try:
            await _write_batch(batch)
        except Exception as e:  # 观测旁路: 本批失败继续消费
            logger.warning("minor writer 批量写入失败(旁路): %s", e)
        finally:
            for _ in batch:
                q.task_done()


async def _write_batch(batch: list[dict]) -> None:
    from lawApp_LangGraph import db

    pool = await db.get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            for op in batch:
                if op.get("op") == "open":
                    await _write_open(conn, op)
                elif op.get("op") == "close":
                    await _write_close(conn, op)
                elif op.get("op") == "route":
                    await _write_route(conn, op)


async def _write_open(conn, op: dict) -> None:
    from lawApp_LangGraph.db import _trace_json

    cur = await conn.execute(
        "INSERT INTO stage_chain (run_id, session_id, node_name, seq) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (op["run_id"], op["session_id"], op["node_name"], op["seq"]),
    )
    stage_id = (await cur.fetchone())[0]
    key = (op["run_id"], op["node_name"], op["seq"])
    _STAGE_IDS[key] = stage_id
    await conn.execute(
        "UPDATE trace_runs SET current_node=%s WHERE run_id=%s",
        (op["node_name"], op["run_id"]),
    )
    if not op.get("minor"):
        return
    parent_stage_id = None
    parent_run = op.get("parent_run_id")
    if parent_run:
        cur = await conn.execute(
            "SELECT id FROM stage_chain WHERE run_id=%s AND node_name=%s "
            "AND status='interrupted' ORDER BY started_at DESC LIMIT 1",
            (parent_run, op["node_name"]),
        )
        row = await cur.fetchone()
        parent_stage_id = row[0] if row else None
    await conn.execute(
        """
        INSERT INTO minor_states
            (stage_id, attempt, phase, input_text, context_meta, parent_stage_id)
        VALUES (%s, %s, 'running', %s, %s, %s)
        ON CONFLICT (stage_id) DO NOTHING
        """,
        (
            stage_id,
            op.get("attempt", 1),
            op.get("input_text"),
            _trace_json(op.get("context_meta") or {}),
            parent_stage_id,
        ),
    )


async def _stage_id_for(conn, op: dict) -> Optional[int]:
    key = (op["run_id"], op["node_name"], op["seq"])
    if key in _STAGE_IDS:
        return _STAGE_IDS[key]
    cur = await conn.execute(
        "SELECT id FROM stage_chain WHERE run_id=%s AND node_name=%s AND seq=%s",
        key,
    )
    row = await cur.fetchone()
    return row[0] if row else None


async def _write_close(conn, op: dict) -> None:
    from lawApp_LangGraph.db import _trace_json

    key = (op["run_id"], op["node_name"], op["seq"])
    stage_id = await _stage_id_for(conn, op)
    if stage_id is None:
        logger.warning("minor close 无 stage 行(旁路): %s", key)
        return
    refs = dict(op.get("refs") or {})
    if op.get("minor"):
        route = _ROUTES.pop(key, None)
        await conn.execute(
            """
            UPDATE minor_states SET phase=%s, output_text=%s, output_json=%s,
                context_meta=%s, metrics=%s, error=%s, refs=%s,
                route=%s, route_reason=%s
            WHERE stage_id=%s
            """,
            (
                op.get("phase", "done"),
                op.get("output_text"),
                _trace_json(op.get("output_json") or {}),
                _trace_json(op.get("context_meta") or {}),
                _trace_json(op.get("metrics") or {}),
                op.get("error"),
                _trace_json(refs),
                route[0] if route else None,
                route[1] if route else None,
                stage_id,
            ),
        )
    await conn.execute(
        "UPDATE stage_chain SET status=%s, ended_at=NOW(), is_current=FALSE, "
        "latency_ms=%s, detail=%s WHERE id=%s",
        (
            op.get("status", "ok"),
            op.get("latency_ms", 0),
            _trace_json(
                {"exception": str(op.get("error") or "")[:300]}
                if op.get("error")
                else {}
            ),
            stage_id,
        ),
    )
    await conn.execute(
        "UPDATE trace_runs SET current_node=NULL WHERE run_id=%s", (op["run_id"],)
    )
    _ROUTES.pop(key, None)


async def _write_route(conn, op: dict) -> None:
    key = (op["run_id"], op["node_name"], op["seq"])
    stage_id = await _stage_id_for(conn, op)
    if stage_id is None:
        # 闭行尚未处理时先缓存, close 时补写
        _ROUTES[key] = (op["route"], op.get("route_reason") or "")
        return
    await conn.execute(
        "UPDATE minor_states SET route=%s, route_reason=%s WHERE stage_id=%s",
        (op["route"], op.get("route_reason") or "", stage_id),
    )


async def drain(timeout: float = 2.0) -> None:
    """等待当前队列落库; 超时放行(旁路)。"""
    q = _QUEUE
    if q is None:
        return
    try:
        await asyncio.wait_for(q.join(), timeout)
    except Exception:
        logger.warning("minor writer drain 超时(旁路)")
