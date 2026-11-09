"""证据规范化与稳定 ID — 证据链在工具调用/文档/终答引用之间共用同一口径.

ID 只依赖 run_id + kind + 文档键, 因此同一次 run 内重复检索到同一文档会
自然去重, 且 finalize 引用无需知道来源 span 即可与工具结果对齐。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

KINDS = ("case", "law", "memory", "web")


def _canon(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _digest(text: str, n: int = 16) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:n]


def doc_key(kind: str, doc: dict) -> str:
    """文档稳定键: 法条用标题+条号, 案例用案号, 记忆用内容 hash."""
    if kind == "law":
        return f"{doc.get('law_title', '')}|{doc.get('article_number', '')}"
    if kind == "case":
        return str(
            doc.get("case_number")
            or doc.get("title")
            or _digest(str(doc.get("chunk_text") or ""), 12)
        )
    if kind == "memory":
        return _digest(str(doc.get("content") or ""))
    return f"{doc.get('title', '')}|{doc.get('link', '')}"


def evidence_id_for(run_id: str, kind: str, key: str) -> str:
    """稳定证据 ID: sha1(run_id:kind:doc_key)[:16]."""
    return _digest(f"{run_id}:{kind}:{key}")


def collect_documents(tool_name: str, result: Any) -> list[dict]:
    """从工具返回 dict 中抽取文档, 统一为 {kind, doc, doc_key}."""
    data = result if isinstance(result, dict) else {}
    out: list[dict] = []
    for kind, field in (
        ("case", "rag_documents"),
        ("law", "law_results"),
        ("memory", "memory_results"),
        ("web", "web_search_results"),
    ):
        for doc in data.get(field) or []:
            if isinstance(doc, dict):
                out.append(
                    {"kind": kind, "doc": doc, "doc_key": doc_key(kind, doc)}
                )
    return out


def collect_prompts_documents(prompts_record: Any) -> list[dict]:
    """从 PromptsRecord 抽取实际注入分析的文档.

    字段名与工具返回不同: 案例在 evaluate_retrieved_documents, 法条在
    laws_results, 记忆/联网沿用原字段; 不能复用 collect_documents。
    """
    data = prompts_record if isinstance(prompts_record, dict) else {}
    out: list[dict] = []
    for kind, field in (
        ("case", "evaluate_retrieved_documents"),
        ("law", "laws_results"),
        ("memory", "memory_results"),
        ("web", "web_search_results"),
    ):
        for doc in data.get(field) or []:
            if isinstance(doc, dict):
                out.append(
                    {"kind": kind, "doc": doc, "doc_key": doc_key(kind, doc)}
                )
    return out


def doc_title(kind: str, doc: dict) -> str:
    """人读标题, 不返回正文."""
    if kind == "law":
        return f"{doc.get('law_title', '')} {doc.get('article_number', '')}".strip()
    if kind == "case":
        return str(
            doc.get("case_number") or doc.get("case_cause") or doc.get("title") or ""
        )[:200]
    if kind == "memory":
        return str(doc.get("content") or "")[:160]
    return str(doc.get("title") or doc.get("link") or "")[:200]


def doc_score(kind: str, doc: dict) -> float | None:
    for key in ("relevance_score", "rrf_score", "similarity", "score"):
        value = doc.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def digest_value(value: Any, n: int = 12) -> str:
    """参数摘要: 不落完整参数, 只给短 hash 供比对."""
    return _digest(_canon(value), n)
