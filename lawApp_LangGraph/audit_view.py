"""统一审计视图 builder — 只读派生, 不参与 Agent 决策.

输入是四张事实表的行(dict), 输出 `AuditView`。builder 不查库, 便于单测:
- plan_round / step_index 从 planner/executor 序列派生
- 证据 ID 由 evidence.py 稳定生成, 同 run 内跨节点去重
- 旧 run 缺 context_meta / evidence_ids 时显式 unknown / partial
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from lawApp_LangGraph.evidence import (
    collect_documents,
    collect_prompts_documents,
    digest_value,
    doc_score,
    doc_title,
    evidence_id_for,
)
from lawApp_LangGraph.FastAPI.audit_model import (
    Anomaly,
    AuditNode,
    AuditNodeDetail,
    AuditVerdicts,
    AuditView,
    CallItem,
    ChainRun,
    CitationEvidence,
    ContextMatrix,
    ContextNode,
    ContextSegmentInfo,
    ContextSummary,
    DecisionDetail,
    DocumentEvidence,
    EvidenceBoard,
    EvidenceGap,
    JumpTarget,
    PerformanceBoard,
    PerformanceError,
    PerformanceSlow,
    RawBlock,
    RunHeader,
    TimelineGroup,
    TokenBoard,
    ToolCallEvidence,
    Verdict,
)

HITL_NODES = {
    "ask_element",
    "field_clarify",
    "risk_confirm",
    "docx_confirm",
    "mid_clarify",
    "degrade_confirm",
    "budget_confirm",
    "hitl_degrade",
    "hitl_budget",
    "element_fill",
}
DECISION_NODES = {"element_assess", "planner", "replan_check", "replanner"}
ANSWER_NODES = {"finalize", "chitchat"}
PLAN_NODES = {"planner", "replanner"}
STEP_NODES = {"executor", "tools", "merge"}
CONTEXT_SEGMENTS = [
    "task",
    "current_query",
    "critical_facts",
    "history",
    "evidence",
    "constraints",
    "memory",
]
NODE_LABELS = {
    "ingest": "初始化",
    "risk_gate": "风险闸门",
    "element_assess": "要素评估",
    "ask_element": "澄清反问",
    "field_clarify": "补充要素",
    "risk_confirm": "风险确认",
    "docx_confirm": "文书确认",
    "mid_clarify": "中途澄清",
    "degrade_confirm": "降级确认",
    "budget_confirm": "预算确认",
    "planner": "生成计划",
    "executor": "执行步骤",
    "tools": "工具调用",
    "merge": "合并结果",
    "replan_check": "结果复核",
    "replanner": "重新规划",
    "hitl_degrade": "降级选择",
    "hitl_budget": "预算选择",
    "element_fill": "补充要素",
    "finalize": "生成终答",
    "chitchat": "闲聊回复",
}

_NOW = lambda: datetime.now(timezone.utc)  # noqa: E731


def _as_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _parse_ts(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _iso(value: Any) -> Optional[str]:
    parsed = _parse_ts(value)
    return parsed.isoformat() if parsed else None


def _ms_between(start: Any, end: Any) -> int:
    a, b = _parse_ts(start), _parse_ts(end)
    if a is None:
        return 0
    if b is None:
        b = _NOW()
    return max(0, int((b - a).total_seconds() * 1000))


def _latency(stage: dict) -> int:
    value = stage.get("latency_ms")
    if value is not None:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            pass
    return _ms_between(stage.get("started_at"), stage.get("ended_at"))


def _minor_of(minors: Any, stage_id: int) -> dict:
    if isinstance(minors, dict):
        return minors.get(stage_id) or {}
    for row in minors or []:
        if row.get("stage_id") == stage_id:
            return row
    return {}


def _output_of(stage: dict, minors: Any) -> dict:
    """minor 输出优先; 纯 builder 单测可直接把摘要放 stage.output_json。"""
    sid = int(stage.get("stage_id") or 0)
    return _as_dict((_minor_of(minors, sid) or {}).get("output_json")) or _as_dict(
        stage.get("output_json")
    )


def _sorted_stages(stages: Iterable[dict]) -> list[dict]:
    def key(s: dict):
        return (_parse_ts(s.get("started_at")) or datetime.min.replace(tzinfo=timezone.utc),
                int(s.get("seq") or 0), int(s.get("stage_id") or 0))

    return sorted(stages or [], key=key)


def _node_kind(name: str) -> str:
    if name in DECISION_NODES:
        return "decision"
    if name in HITL_NODES:
        return "hitl"
    if name in ANSWER_NODES:
        return "answer"
    if name in STEP_NODES:
        return "step"
    return "decision"


def _step_value(output: dict) -> Optional[int]:
    value = output.get("step_index")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def derive_node_meta(
    stages: list[dict], minors_by_stage: Any
) -> dict[int, dict]:
    """stage_id → {plan_round, step_index}; 跨 run 链连续。"""
    plan_round = 0
    last_step: Optional[int] = None
    out: dict[int, dict] = {}
    for stage in _sorted_stages(stages):
        name = stage.get("node_name") or ""
        sid = int(stage.get("stage_id") or 0)
        output = _output_of(stage, minors_by_stage)
        if name in PLAN_NODES:
            plan_round += 1
            last_step = None
        step: Optional[int] = None
        if name == "executor":
            step = _step_value(output)
            if step is not None:
                last_step = step
        elif name in ("tools", "merge"):
            step = _step_value(output)
            if step is None:
                step = last_step
        out[sid] = {"plan_round": plan_round, "step_index": step}
    return out


def _group_label(kind: str, nodes: list[dict], meta: dict) -> str:
    first = nodes[0]
    name = first.get("node_name") or ""
    if kind == "step":
        step = meta.get("step_index")
        tool = ""
        for node in nodes:
            output = _as_dict(node.get("_output"))
            if output.get("tool"):
                tool = str(output.get("tool"))
                break
        prefix = f"步骤 {step + 1}" if step is not None else "执行步骤"
        return f"{prefix} · {tool}" if tool else prefix
    if kind == "answer":
        return "生成终答" if name == "finalize" else "闲聊回复"
    if kind == "hitl":
        return NODE_LABELS.get(name, name)
    return NODE_LABELS.get(name, name or "决策")


def group_timeline(
    stages: list[dict], minors_by_stage: Any
) -> list[TimelineGroup]:
    """把 stage 序列折叠为 decision / step / hitl / answer 组。"""
    metas = derive_node_meta(stages, minors_by_stage)
    ordered = _sorted_stages(stages)
    groups: list[dict] = []
    by_key: dict[str, dict] = {}

    for stage in ordered:
        sid = int(stage.get("stage_id") or 0)
        run_id = str(stage.get("run_id") or "")
        name = stage.get("node_name") or ""
        minor = _minor_of(minors_by_stage, sid)
        output = _output_of(stage, minors_by_stage)
        meta = metas.get(sid, {})
        kind = _node_kind(name)
        if kind == "step":
            step = meta.get("step_index")
            key = (
                f"{run_id}:step:{meta.get('plan_round')}:{step}"
                if step is not None
                else f"{run_id}:step:nostep:{sid}"
            )
        else:
            key = f"{run_id}:{kind}:{sid}"

        node = {
            **stage,
            "_output": output,
            "_minor": minor,
            "_meta": meta,
            "_kind": kind,
        }
        if key in by_key:
            by_key[key]["nodes"].append(node)
        else:
            group = {"key": key, "kind": kind, "nodes": [node]}
            by_key[key] = group
            groups.append(group)

    out: list[TimelineGroup] = []
    for group in groups:
        nodes = group["nodes"]
        first, last = nodes[0], nodes[-1]
        meta = first["_meta"]
        refs_ids: set[str] = set()
        errors = 0
        statuses = []
        for node in nodes:
            refs = _as_dict((node["_minor"] or {}).get("refs"))
            ids = refs.get("evidence_ids") or []
            refs_ids.update(str(x) for x in ids if x)
            statuses.append((node.get("status") or "running").lower())
            if (node.get("status") or "") == "error" or (
                node["_minor"] or {}
            ).get("error"):
                errors += 1
        if "error" in statuses:
            status = "error"
        elif "interrupted" in statuses:
            status = "interrupted"
        elif "running" in statuses:
            status = "running"
        else:
            status = "ok"
        out.append(
            TimelineGroup(
                group_id=group["key"],
                kind=group["kind"],
                plan_round=int(meta.get("plan_round") or 0),
                step_index=meta.get("step_index"),
                label=_group_label(group["kind"], nodes, meta),
                stage_ids=[int(n.get("stage_id") or 0) for n in nodes],
                node_names=[str(n.get("node_name") or "") for n in nodes],
                started_at=_iso(first.get("started_at")),
                ended_at=_iso(last.get("ended_at")) if last.get("ended_at") else None,
                latency_ms=sum(_latency(n) for n in nodes),
                evidence_count=len(refs_ids),
                error_count=errors,
                status=status,
            )
        )
    return out


def _decision_summary(name: str, output: dict, text: str) -> str:
    decision = _as_dict(output.get("decision"))
    if decision.get("summary"):
        return str(decision["summary"])[:200]
    if name in PLAN_NODES:
        return f"计划 {output.get('step_count') or len(output.get('steps') or [])} 步"
    if name == "executor":
        if output.get("tool"):
            return f"调用 {output.get('tool')}"
        idx = output.get("step_index")
        return f"推进步骤 {int(idx) + 1}" if isinstance(idx, int) else "推进步骤"
    if name == "tools":
        return str(text or "工具调用")[:160]
    if name == "merge":
        return str(text or "合并结果")[:160]
    if name == "replan_check":
        return "需要重规划" if output.get("replan_needed") else "无需重规划"
    if name == "replanner":
        return f"补充 {output.get('step_count') or len(output.get('steps') or [])} 步"
    if name == "element_assess":
        pending = output.get("pending_questions") or []
        return f"待补 {len(pending)} 项" if pending else "要素齐全"
    if name in ANSWER_NODES:
        return f"回答 {output.get('answer_len') or 0} 字"
    return str(text or "")[:160]


def build_nodes(stages: list[dict], minors_by_stage: Any) -> list[AuditNode]:
    """stage 行 → 审计节点摘要。"""
    metas = derive_node_meta(stages, minors_by_stage)
    out: list[AuditNode] = []
    for stage in _sorted_stages(stages):
        sid = int(stage.get("stage_id") or 0)
        name = str(stage.get("node_name") or "")
        minor = _minor_of(minors_by_stage, sid)
        output = _output_of(stage, minors_by_stage)
        refs = _as_dict(minor.get("refs"))
        meta = metas.get(sid, {})
        context_meta = _as_dict(minor.get("context_meta"))
        flags: list[str] = []
        if not context_meta:
            flags.append("missing")
        if context_meta.get("truncated"):
            flags.append("truncated")
        evidence_ids = refs.get("evidence_ids") or []
        citations = output.get("citations") or []
        out.append(
            AuditNode(
                stage_id=sid,
                node_exec_id=f"{stage.get('run_id')}:{name}:{stage.get('seq')}",
                run_id=str(stage.get("run_id") or ""),
                node_name=name,
                seq=int(stage.get("seq") or 1),
                plan_round=int(meta.get("plan_round") or 0),
                step_index=meta.get("step_index"),
                attempt=int(minor.get("attempt") or 1),
                phase=str(minor.get("phase") or "done"),
                status=str(stage.get("status") or "running"),
                started_at=_iso(stage.get("started_at")),
                ended_at=_iso(stage.get("ended_at")) if stage.get("ended_at") else None,
                latency_ms=_latency(stage),
                route=minor.get("route"),
                route_reason=minor.get("route_reason"),
                decision_summary=_decision_summary(
                    name, output, str(minor.get("output_text") or "")
                ),
                evidence_count=len(evidence_ids) if isinstance(evidence_ids, list) else 0,
                citation_count=len(citations) if isinstance(citations, list) else 0,
                context_flags=flags,
                error=str(minor.get("error") or "") or None,
                content_source=str(minor.get("content_source") or "projected"),
            )
        )
    return out


def _stage_for_span(
    span: dict, stages: list[dict], tool_only: bool = True
) -> Optional[int]:
    """按时间区间把 span 归到同 run 的节点(stage)。"""
    run_id = span.get("run_id")
    started = _parse_ts(span.get("started_at"))
    candidates = [
        s
        for s in stages
        if s.get("run_id") == run_id
        and (not tool_only or (s.get("node_name") in ("tools", "executor")))
    ]
    if span.get("span_type") == "tool":
        # tool span 常在 tools 节点 span 开启前几毫秒启动, 落在 executor 尾部;
        # 按节点语义只归 tools, 避免证据挂到决策节点上
        tool_stages = [s for s in candidates if s.get("node_name") == "tools"]
        if tool_stages:
            candidates = tool_stages
    if not candidates:
        candidates = [s for s in stages if s.get("run_id") == run_id]
    if not candidates:
        return None
    if started is None:
        return int(candidates[0].get("stage_id") or 0) or None
    containing = []
    for stage in candidates:
        a = _parse_ts(stage.get("started_at"))
        b = _parse_ts(stage.get("ended_at"))
        if a and a <= started and (b is None or started <= b):
            containing.append(stage)
    pool = containing or candidates
    best = min(
        pool,
        key=lambda s: abs(
            (started - (_parse_ts(s.get("started_at")) or started)).total_seconds()
        ),
    )
    return int(best.get("stage_id") or 0) or None


def _span_output(span: dict) -> dict:
    return _as_dict(span.get("output"))


def _counts_from_output(output: dict) -> dict:
    fields = {
        "laws": "law_results",
        "cases": "rag_documents",
        "memory": "memory_results",
        "web": "web_search_results",
    }
    counts = {k: len(output.get(v) or []) for k, v in fields.items()}
    if not any(counts.values()):
        # analyze_legal_issue 结果不带工具字段, 只有 prompts_record;
        # 按实际注入分析的来源计数, 避免工具卡显示 0/0/0/0
        pr = _as_dict(output.get("prompts_record"))
        if pr:
            used = collect_prompts_documents(pr)
            counts = {
                k: sum(1 for d in used if d["kind"] == kind)
                for k, kind in (
                    ("laws", "law"),
                    ("cases", "case"),
                    ("memory", "memory"),
                    ("web", "web"),
                )
            }
    return counts


def _citation_rows(stages: list[dict], minors_by_stage: Any) -> list[dict]:
    rows: list[dict] = []
    for stage in _sorted_stages(stages):
        name = stage.get("node_name")
        if name not in ANSWER_NODES:
            continue
        output = _output_of(stage, minors_by_stage)
        for cite in output.get("citations") or []:
            if isinstance(cite, dict) and cite.get("label"):
                rows.append(cite)
    return rows


def build_evidence(
    run: dict,
    stages: list[dict],
    minors: Any,
    spans: list[dict],
) -> EvidenceBoard:
    """工具 span → 文档 → used_in_* → 终答引用/缺口。"""
    run_id = str(run.get("run_id") or "")
    used_ids: set[str] = set()
    has_used_field = False
    for stage in stages:
        refs = _as_dict((_minor_of(minors, int(stage.get("stage_id") or 0)) or {}).get("refs"))
        if "evidence_ids_used" in refs:
            has_used_field = True
            used_ids.update(str(x) for x in refs.get("evidence_ids_used") or [] if x)

    cites = _citation_rows(stages, minors)
    cite_by_evidence: dict[str, str] = {}
    citations: list[CitationEvidence] = []
    for cite in cites:
        ids = [str(x) for x in (cite.get("evidence_ids") or []) if x]
        resolved = bool(cite.get("resolved", bool(ids)))
        citations.append(
            CitationEvidence(
                label=str(cite.get("label") or "")[:300],
                type=str(cite.get("type") or "case"),
                evidence_ids=ids,
                resolved=resolved,
            )
        )
        for eid in ids:
            cite_by_evidence[eid] = str(cite.get("label") or "")
    any_cite_with_ids = bool(cite_by_evidence)

    tool_calls: list[ToolCallEvidence] = []
    docs: dict[str, DocumentEvidence] = {}
    for span in sorted(spans or [], key=lambda s: _parse_ts(s.get("started_at")) or _NOW()):
        if span.get("span_type") != "tool":
            continue
        output = _span_output(span)
        sid = int(span.get("span_id") or span.get("id") or 0) or None
        stage_id = _stage_for_span(span, stages)
        tool_calls.append(
            ToolCallEvidence(
                span_id=sid,
                stage_id=stage_id,
                tool=str(span.get("name") or ""),
                args_digest=digest_value(span.get("input")),
                status=span.get("status"),
                latency_ms=span.get("latency_ms"),
                counts=_counts_from_output(output),
                result_ref=f"span:{sid}" if sid else "",
            )
        )
        verdict = _as_dict(output.get("evaluation")).get("quality_verdict")
        for item in collect_documents(str(span.get("name") or ""), output):
            eid = evidence_id_for(run_id, item["kind"], item["doc_key"])
            used_analysis = True if eid in used_ids else (
                False if has_used_field else None
            )
            used_answer = True if eid in cite_by_evidence else (
                False if any_cite_with_ids else None
            )
            doc = DocumentEvidence(
                evidence_id=eid,
                kind=item["kind"],
                title=doc_title(item["kind"], item["doc"]),
                score=doc_score(item["kind"], item["doc"]),
                verdict=verdict,
                source_span_id=sid,
                source_stage_id=stage_id,
                used_in_analysis=used_analysis,
                used_in_answer=used_answer,
            )
            old = docs.get(eid)
            if old is None or (doc.score or 0) > (old.score or 0):
                docs[eid] = doc

    documents = sorted(
        docs.values(), key=lambda d: (d.used_in_answer is True, d.score or 0), reverse=True
    )
    gaps: list[EvidenceGap] = []
    for doc in documents:
        if doc.used_in_analysis is False and len(gaps) < 10:
            gaps.append(
                EvidenceGap(
                    code="retrieved_not_used",
                    label=f"检索到但未注入分析: {doc.title}",
                    stage_id=doc.source_stage_id,
                    evidence_id=doc.evidence_id,
                )
            )
    for cite in citations:
        if not cite.resolved and len(gaps) < 20:
            gaps.append(
                EvidenceGap(
                    code="cited_without_source",
                    label=f"引用无法回溯证据: {cite.label}",
                    evidence_id=cite.evidence_ids[0] if cite.evidence_ids else None,
                )
            )

    expected = bool(
        [s for s in stages if s.get("node_name") in ("tools", "executor")]
        or tool_calls
    )
    if expected and not documents:
        gaps.append(
            EvidenceGap(code="no_evidence", label="执行过检索/工具但没有可用证据")
        )

    return EvidenceBoard(
        tool_calls=tool_calls[:200],
        documents=documents[:50],
        citations=citations[:50],
        gaps=gaps[:50],
        truncated=len(tool_calls) > 200 or len(documents) > 50,
    )


def _segment_info(value: Any, truncated: bool = False) -> ContextSegmentInfo:
    data = _as_dict(value)
    # memory 段合并进 history 时显式 present=false, 只带 hits
    present = (
        bool(data.get("present")) if "present" in data else bool(data or isinstance(value, dict))
    )
    try:
        chars = int(data.get("chars") or 0)
    except (TypeError, ValueError):
        chars = 0
    return ContextSegmentInfo(
        present=present,
        chars=chars,
        hash=str(data.get("hash") or ""),
        truncated=truncated,
        injected=present,
    )


def _segments_of(meta: dict) -> dict[str, ContextSegmentInfo]:
    paths = [
        str(x) for x in (meta.get("truncated_segments") or []) if isinstance(x, str)
    ]
    # 旧 meta 只有全局 truncated 布尔, 无法定位到段 → 不伪造段级标记
    return {
        seg: _segment_info(
            meta.get(seg),
            any(p == seg or p.startswith(f"{seg}.") for p in paths),
        )
        for seg in CONTEXT_SEGMENTS
    }


def build_context(stages: list[dict], minors: Any) -> ContextMatrix:
    """节点 × 上下文段; 无 meta 节点继承上游并标 injected=false。"""
    nodes: list[ContextNode] = []
    last_meta_node: Optional[ContextNode] = None
    last_meta_segments: Optional[dict[str, ContextSegmentInfo]] = None
    last_meta_hash: dict[str, str] = {}
    truncated: set[str] = set()
    total_chars = 0
    history_turns: Optional[int] = None
    memory_hits = 0

    for stage in _sorted_stages(stages):
        sid = int(stage.get("stage_id") or 0)
        name = str(stage.get("node_name") or "")
        meta = _as_dict((_minor_of(minors, sid) or {}).get("context_meta"))
        if meta:
            segments = _segments_of(meta)
            diff = {"added": [], "removed": []}
            for seg in CONTEXT_SEGMENTS:
                info = segments[seg]
                if info.present:
                    prev = last_meta_hash.get(seg)
                    if prev != info.hash:
                        diff["added"].append(seg)
                    if info.truncated:
                        truncated.add(seg)
                elif seg in last_meta_hash:
                    diff["removed"].append(seg)
            last_meta_hash = {
                seg: info.hash for seg, info in segments.items() if info.present
            }
            total_chars += sum(
                info.chars for info in segments.values() if info.injected
            )
            hist = _as_dict(meta.get("history"))
            if hist.get("turns") is not None:
                try:
                    history_turns = int(hist["turns"])
                except (TypeError, ValueError):
                    pass
            mem = _as_dict(meta.get("memory"))
            if mem.get("hits") is not None:
                try:
                    memory_hits = max(memory_hits, int(mem["hits"]))
                except (TypeError, ValueError):
                    pass
            try:
                memory_hits = max(
                    memory_hits, int(_output_of(stage, minors).get("memory_count") or 0)
                )
            except (TypeError, ValueError):
                pass
            node = ContextNode(
                stage_id=sid,
                node_name=name,
                segments=segments,
                diff=diff,
                inherited_from_stage_id=None,
            )
            last_meta_node = node
            last_meta_segments = segments
        else:
            if last_meta_segments:
                segments = {
                    seg: info.model_copy(update={"injected": False})
                    for seg, info in last_meta_segments.items()
                }
                node = ContextNode(
                    stage_id=sid,
                    node_name=name,
                    segments=segments,
                    diff={"added": [], "removed": []},
                    inherited_from_stage_id=(
                        last_meta_node.stage_id if last_meta_node else None
                    ),
                )
            else:
                node = ContextNode(
                    stage_id=sid,
                    node_name=name,
                    segments={
                        seg: ContextSegmentInfo() for seg in CONTEXT_SEGMENTS
                    },
                    diff={"added": [], "removed": []},
                    inherited_from_stage_id=None,
                )
        nodes.append(node)

    return ContextMatrix(
        segments=CONTEXT_SEGMENTS,
        nodes=nodes,
        summary=ContextSummary(
            total_chars=total_chars,
            truncated_segments=sorted(truncated),
            history_turns=history_turns,
            memory_hits=memory_hits,
        ),
    )


def _token_tuple(value: Any) -> Tuple[int, int]:
    data = _as_dict(value)
    prompt = int(data.get("prompt") or data.get("prompt_tokens") or 0)
    completion = int(data.get("completion") or data.get("completion_tokens") or 0)
    return prompt, completion


def build_performance(
    run: dict,
    chain: list[dict],
    stages: list[dict],
    minors: Any,
    spans: list[dict],
    slow_ms: int = 30000,
) -> PerformanceBoard:
    """耗时构成、慢点、错误、tokens、重试、触顶。"""
    runs = chain or [run]
    first_start = min(
        (_parse_ts(r.get("started_at")) for r in runs if _parse_ts(r.get("started_at"))),
        default=None,
    )
    last_end = max(
        (_parse_ts(r.get("ended_at")) for r in runs if _parse_ts(r.get("ended_at"))),
        default=None,
    )
    total_ms = _ms_between(first_start, last_end or _NOW())
    execution_ms = sum(_latency(s) for s in stages)
    wait_human_ms = 0
    ordered_runs = sorted(
        runs, key=lambda r: _parse_ts(r.get("started_at")) or datetime.min.replace(tzinfo=timezone.utc)
    )
    for prev, nxt in zip(ordered_runs, ordered_runs[1:]):
        wait_human_ms += _ms_between(prev.get("ended_at"), nxt.get("started_at"))
    overhead_ms = max(0, total_ms - execution_ms - wait_human_ms)

    slowest: list[PerformanceSlow] = []
    for stage in stages:
        latency = _latency(stage)
        slowest.append(
            PerformanceSlow(
                stage_id=int(stage.get("stage_id") or 0),
                node_name=str(stage.get("node_name") or ""),
                latency_ms=latency,
                pct_of_execution=(
                    round(latency / execution_ms * 100, 2) if execution_ms else 0.0
                ),
            )
        )
    slowest = sorted(slowest, key=lambda x: x.latency_ms, reverse=True)[:3]

    errors: list[PerformanceError] = []
    retries = 0
    for stage in stages:
        sid = int(stage.get("stage_id") or 0)
        minor = _minor_of(minors, sid) or {}
        attempt = int(minor.get("attempt") or 1)
        if attempt > 1:
            retries += 1
        err = str(minor.get("error") or "")
        if err or (stage.get("status") or "") == "error":
            errors.append(
                PerformanceError(
                    stage_id=sid,
                    node_name=str(stage.get("node_name") or ""),
                    error=(err or "节点状态 error")[:300],
                    retry=max(0, attempt - 1),
                )
            )
    for span in spans or []:
        if span.get("span_type") == "tool" and span.get("status") not in (None, "ok"):
            errors.append(
                PerformanceError(
                    stage_id=_stage_for_span(span, stages),
                    node_name=str(span.get("name") or ""),
                    error=f"工具状态 {span.get('status')}",
                    retry=0,
                )
            )

    tokens = TokenBoard()
    by_node: dict[str, int] = {}
    for stage in stages:
        sid = int(stage.get("stage_id") or 0)
        metrics = _as_dict((_minor_of(minors, sid) or {}).get("metrics"))
        tk = _as_dict(metrics.get("tokens"))
        total = int(tk.get("total") or 0)
        if total:
            by_node[str(sid)] = total
            tokens.prompt += int(tk.get("prompt") or 0)
            tokens.completion += int(tk.get("completion") or 0)
            tokens.total += total
    if not tokens.total:
        for span in spans or []:
            prompt, completion = _token_tuple(span.get("token_usage"))
            if not prompt and not completion:
                continue
            sid = _stage_for_span(span, stages)
            key = str(sid or 0)
            by_node[key] = by_node.get(key, 0) + prompt + completion
            tokens.prompt += prompt
            tokens.completion += completion
        tokens.total = tokens.prompt + tokens.completion
    tokens.by_node = by_node

    metrics = _as_dict(run.get("metrics"))
    limit_hit = metrics.get("limit_hit") in (True, "true", 1, "1")

    return PerformanceBoard(
        total_ms=max(0, total_ms),
        execution_ms=execution_ms,
        wait_human_ms=wait_human_ms,
        overhead_ms=overhead_ms,
        slowest=slowest,
        errors=errors[:50],
        tokens=tokens,
        retries=retries,
        limit_hit=limit_hit,
    )


def _old_run_partial(
    stages: list[dict], minors: Any, evidence: EvidenceBoard, has_citations: bool
) -> bool:
    """旧 run 缺证据关联/上下文/citations 任一关键采集键。"""
    if not stages:
        return False
    has_evidence_field = False
    has_context = False
    for stage in stages:
        minor = _minor_of(minors, int(stage.get("stage_id") or 0)) or {}
        refs = _as_dict(minor.get("refs"))
        if refs.get("evidence_ids") or refs.get("evidence_ids_used"):
            has_evidence_field = True
        if _as_dict(minor.get("context_meta")):
            has_context = True
    return bool(
        (evidence.documents and not has_evidence_field and not has_citations)
        or not has_context
    )


def collect_anomalies(
    run: dict,
    chain: list[dict],
    stages: list[dict],
    minors: Any,
    spans: list[dict],
    evidence: EvidenceBoard,
    context: ContextMatrix,
    performance: PerformanceBoard,
    stale_ms: int = 7200000,
) -> list[Anomaly]:
    """固定 code 的异常/缺口; verdict 只消费这里与统计量。"""
    out: list[Anomaly] = []
    run_id = str(run.get("run_id") or "")

    for stage in stages:
        sid = int(stage.get("stage_id") or 0)
        minor = _minor_of(minors, sid) or {}
        if (stage.get("status") or "") == "error" or minor.get("error"):
            out.append(
                Anomaly(
                    code="node_error",
                    severity="error",
                    title=f"节点失败: {stage.get('node_name')}",
                    detail=str(minor.get("error") or "节点状态 error")[:300],
                    run_id=str(stage.get("run_id") or run_id),
                    stage_id=sid,
                )
            )
        if not minor or minor.get("content_source") == "fallback_placeholder":
            if len([a for a in out if a.code == "minor_missing"]) < 20:
                out.append(
                    Anomaly(
                        code="minor_missing",
                        severity="warn",
                        title=f"内容投影缺失: {stage.get('node_name')}",
                        detail="stage_chain 有行但 minor_states 无内容, 已用占位",
                        run_id=str(stage.get("run_id") or run_id),
                        stage_id=sid,
                    )
                )

    for span in spans or []:
        if span.get("span_type") == "tool" and span.get("status") not in (None, "ok"):
            out.append(
                Anomaly(
                    code="tool_error",
                    severity="error",
                    title=f"工具失败: {span.get('name')}",
                    detail=f"status={span.get('status')}",
                    run_id=str(span.get("run_id") or run_id),
                    stage_id=_stage_for_span(span, stages),
                )
            )

    replan = any(
        s.get("node_name") in ("replanner",)
        or (
            s.get("node_name") == "replan_check"
            and _as_dict((_minor_of(minors, int(s.get("stage_id") or 0)) or {}).get("output_json")).get("replan_needed")
        )
        for s in stages
    )
    if replan:
        out.append(
            Anomaly(
                code="replan_triggered",
                severity="warn",
                title="触发重新规划",
                detail="执行过程中判定证据不足并回环",
                run_id=run_id,
            )
        )

    if any(g.code == "no_evidence" for g in evidence.gaps):
        out.append(
            Anomaly(
                code="evidence_insufficient",
                severity="warn",
                title="证据不足",
                detail="执行过检索但没有可用文档",
                run_id=run_id,
            )
        )
    unresolved = [c for c in evidence.citations if not c.resolved]
    if unresolved:
        out.append(
            Anomaly(
                code="citation_unresolved",
                severity="warn",
                title=f"{len(unresolved)} 条引用无法回溯证据",
                detail=unresolved[0].label,
                run_id=run_id,
            )
        )
    if any(g.code == "retrieved_not_used" for g in evidence.gaps):
        out.append(
            Anomaly(
                code="evidence_insufficient",
                severity="info",
                title="存在检索到但未注入分析的文档",
                detail="见证据板缺口列表",
                run_id=run_id,
            )
        )

    if context.summary.truncated_segments:
        out.append(
            Anomaly(
                code="context_truncated",
                severity="warn",
                title="上下文段被预算截断",
                detail=", ".join(context.summary.truncated_segments),
                run_id=run_id,
            )
        )
    key_nodes = [
        s
        for s in stages
        if s.get("node_name") in ("planner", "executor", "finalize", "chitchat")
    ]
    missing_ctx = [
        s
        for s in key_nodes
        if not _as_dict((_minor_of(minors, int(s.get("stage_id") or 0)) or {}).get("context_meta"))
    ]
    if key_nodes and missing_ctx:
        out.append(
            Anomaly(
                code="context_missing",
                severity="warn",
                title=f"{len(missing_ctx)} 个关键节点缺少上下文 meta",
                detail=", ".join(str(s.get("node_name")) for s in missing_ctx[:6]),
                run_id=run_id,
                stage_id=int(missing_ctx[0].get("stage_id") or 0) or None,
            )
        )

    if stages and not spans:
        out.append(
            Anomaly(
                code="span_missing",
                severity="warn",
                title="trace_spans 缺失",
                detail="调用/证据只能从 minor_states 摘要推断",
                run_id=run_id,
            )
        )

    waiting = [
        s
        for s in stages
        if (_minor_of(minors, int(s.get("stage_id") or 0)) or {}).get("phase")
        == "waiting_human"
        or (s.get("status") or "") == "interrupted"
    ]
    if waiting:
        out.append(
            Anomaly(
                code="hitl_waiting",
                severity="info",
                title="等待人工输入",
                detail=f"{len(waiting)} 个节点处于 HITL 等待",
                run_id=run_id,
                stage_id=int(waiting[0].get("stage_id") or 0) or None,
            )
        )

    if performance.limit_hit:
        out.append(
            Anomaly(
                code="limit_hit",
                severity="error",
                title="触顶运行",
                detail="达到工具/轮次上限",
                run_id=run_id,
            )
        )

    started = _parse_ts(run.get("started_at"))
    if (run.get("status") or "") == "running" and started:
        if (_NOW() - started).total_seconds() * 1000 > stale_ms:
            out.append(
                Anomaly(
                    code="stale_run",
                    severity="warn",
                    title="运行可能已失联",
                    detail="running 状态超过阈值",
                    run_id=run_id,
                )
            )

    has_citations = bool(evidence.citations)
    if _old_run_partial(stages, minors, evidence, has_citations):
        out.append(
            Anomaly(
                code="audit_partial",
                severity="info",
                title="部分可审计",
                detail="旧 run 缺少证据关联/上下文采集, 未知项不伪造",
                run_id=run_id,
            )
        )

    return out


def _verdict(
    key: str, level: str, summary: str, reasons: list[str], section: str
) -> Verdict:
    return Verdict(
        key=key,
        level=level,  # type: ignore[arg-type]
        summary=summary,
        reasons=[r for r in reasons if r][:5],
        jump=JumpTarget(section=section),
    )


def derive_verdicts(
    run: dict,
    stages: list[dict],
    evidence: EvidenceBoard,
    context: ContextMatrix,
    performance: PerformanceBoard,
    anomalies: list[Anomaly],
    slow_ms: int = 30000,
) -> AuditVerdicts:
    """四张结论卡; 页面不再自行推导第二套结论。"""
    codes = {a.code for a in anomalies}
    status = str(run.get("status") or "unknown")

    if status in ("error", "cancelled") or "node_error" in codes:
        flow = _verdict("flow", "bad", "流程失败或有节点错误", ["存在 error 节点"], "timeline")
    elif status == "running":
        flow = _verdict("flow", "unknown", "运行中", ["结果尚未落定"], "timeline")
    elif "replan_triggered" in codes or performance.retries:
        flow = _verdict(
            "flow", "warn", "流程完成但发生过回环/重试",
            ["触发 replan" if "replan_triggered" in codes else "",
             f"{performance.retries} 个节点重试" if performance.retries else ""],
            "timeline",
        )
    elif "hitl_waiting" in codes:
        flow = _verdict("flow", "good", "等待人工后继续", ["HITL 等待不扣分"], "timeline")
    else:
        flow = _verdict("flow", "good", "流程正常完成", [], "timeline")

    no_docs = not evidence.documents
    unresolved = [c for c in evidence.citations if not c.resolved]
    mode = str(run.get("mode") or "")
    if mode == "assistant" and no_docs and not evidence.citations:
        ev_level, ev_sum = "na", "文书模式无需检索"
        ev_reasons: list[str] = []
    elif unresolved:
        ev_level, ev_sum = "warn", f"{len(unresolved)} 条引用未解析"
        ev_reasons = [c.label for c in unresolved[:3]]
    elif no_docs and evidence.tool_calls:
        ev_level, ev_sum = "bad", "执行过工具但没有可用证据"
        ev_reasons = ["检索结果为空"]
    elif no_docs and not evidence.tool_calls:
        ev_level, ev_sum = "na", "本次运行未涉及检索"
        ev_reasons = []
    elif (
        evidence.documents
        and not any(d.used_in_analysis is True for d in evidence.documents)
        and not evidence.citations
    ):
        if "audit_partial" in codes:
            ev_level, ev_sum = "unknown", "旧 run 缺少证据关联字段"
            ev_reasons = ["无法判断文档是否被分析/引用使用"]
        else:
            ev_level, ev_sum = "warn", "检索到文档但未建立分析/引用关联"
            ev_reasons = [f"{len(evidence.documents)} 份文档均未标记使用"]
    elif any(g.code == "retrieved_not_used" for g in evidence.gaps):
        ev_level, ev_sum = "warn", "存在检索到但未注入分析的文档"
        ev_reasons = [g.label for g in evidence.gaps if g.code == "retrieved_not_used"][:3]
    elif "audit_partial" in codes:
        ev_level, ev_sum = "unknown", "旧 run 缺少证据关联字段"
        ev_reasons = ["无法判断文档是否被分析/引用使用"]
    else:
        ev_level = "good"
        ev_sum = "证据链完整：检索 → 分析与引用可回溯"
        ev_reasons = [f"{len(evidence.documents)} 份文档, {len(evidence.citations)} 条引用"]
    evidence_v = _verdict("evidence", ev_level, ev_sum, ev_reasons, "evidence")

    if not any(
        info.present for n in context.nodes for info in n.segments.values()
    ):
        ctx_level, ctx_sum = "unknown", "没有可用的上下文采集"
        ctx_reasons: list[str] = []
    elif context.summary.truncated_segments:
        ctx_level, ctx_sum = "warn", "上下文有预算截断"
        ctx_reasons = [", ".join(context.summary.truncated_segments)]
    elif "context_missing" in codes:
        ctx_level, ctx_sum = "warn", "部分关键节点缺少上下文 meta"
        ctx_reasons = ["见异常列表"]
    else:
        ctx_level = "good"
        ctx_sum = "上下文段齐全且未截断"
        ctx_reasons = [
            f"累计 {context.summary.total_chars} 字",
            f"历史 {context.summary.history_turns or 0} 轮",
        ]
    context_v = _verdict("context", ctx_level, ctx_sum, ctx_reasons, "context")

    if performance.limit_hit or "node_error" in codes:
        perf_level, perf_sum = "bad", "存在错误或触顶"
        perf_reasons = ["触顶" if performance.limit_hit else "节点错误"]
    elif performance.retries or (
        performance.slowest and performance.slowest[0].latency_ms >= slow_ms
    ):
        perf_level, perf_sum = "warn", "存在慢点或重试"
        perf_reasons = [
            f"最慢 {performance.slowest[0].node_name} "
            f"{performance.slowest[0].latency_ms}ms" if performance.slowest else "",
            f"{performance.retries} 个节点重试" if performance.retries else "",
        ]
    else:
        perf_level = "good"
        perf_sum = "无错误, 慢点低于阈值"
        perf_reasons = [f"总耗时 {performance.total_ms}ms"]
    performance_v = _verdict(
        "performance", perf_level, perf_sum, perf_reasons, "performance"
    )

    return AuditVerdicts(
        flow=flow, evidence=evidence_v, context=context_v, performance=performance_v
    )


def _chain_runs(runs: list[dict], stages: list[dict], minors: Any) -> list[ChainRun]:
    ordered = sorted(
        runs, key=lambda r: _parse_ts(r.get("started_at")) or datetime.min.replace(tzinfo=timezone.utc)
    )
    out: list[ChainRun] = []
    prev: Optional[dict] = None
    for run in ordered:
        sid = str(run.get("run_id") or "")
        own = [s for s in stages if s.get("run_id") == sid]
        has_content = any(
            _as_dict((_minor_of(minors, int(s.get("stage_id") or 0)) or {}).get("context_meta"))
            or _as_dict((_minor_of(minors, int(s.get("stage_id") or 0)) or {}).get("refs"))
            for s in own
        )
        out.append(
            ChainRun(
                run_id=sid,
                parent_run_id=run.get("parent_run_id"),
                status=str(run.get("status") or "unknown"),
                started_at=_iso(run.get("started_at")),
                ended_at=_iso(run.get("ended_at")) if run.get("ended_at") else None,
                latency_ms=_ms_between(run.get("started_at"), run.get("ended_at")),
                wait_before_ms=(
                    _ms_between(prev.get("ended_at"), run.get("started_at"))
                    if prev else 0
                ),
                stage_count=len(own),
                has_audit_content=has_content,
            )
        )
        prev = run
    return out


def build_audit_view(
    run_id: str,
    chain: bool,
    runs: list[dict],
    stages: list[dict],
    minors: Any,
    spans: list[dict],
    *,
    slow_ms: int = 30000,
    stale_ms: int = 7200000,
    after_stage_id: int = 0,
) -> AuditView:
    """入口: 行集合 → 统一审计视图。"""
    ordered_runs = sorted(
        runs or [], key=lambda r: _parse_ts(r.get("started_at")) or datetime.min.replace(tzinfo=timezone.utc)
    )
    current = next((r for r in ordered_runs if r.get("run_id") == run_id), None)
    if current is None:
        current = ordered_runs[-1] if ordered_runs else {"run_id": run_id}
    chain_runs = _chain_runs(ordered_runs, stages, minors)
    timeline = group_timeline(stages, minors)
    if after_stage_id:
        timeline = [
            g for g in timeline
            if any(sid > after_stage_id for sid in g.stage_ids)
        ]
    evidence = build_evidence(current, stages, minors, spans)
    context = build_context(stages, minors)
    performance = build_performance(
        current, ordered_runs, stages, minors, spans, slow_ms=slow_ms
    )
    anomalies = collect_anomalies(
        current, ordered_runs, stages, minors, spans, evidence, context,
        performance, stale_ms=stale_ms,
    )
    verdicts = derive_verdicts(
        current, stages, evidence, context, performance, anomalies, slow_ms=slow_ms
    )
    answer = str(current.get("final_answer") or "")
    root = chain_runs[0].run_id if chain_runs else str(current.get("run_id") or run_id)
    degraded = any(
        a.code in ("audit_partial", "minor_missing", "span_missing") for a in anomalies
    )
    return AuditView(
        generated_at=_NOW().isoformat(),
        degraded=degraded,
        run=RunHeader(
            run_id=str(current.get("run_id") or run_id),
            session_id=current.get("session_id"),
            parent_run_id=current.get("parent_run_id"),
            root_run_id=root,
            run_type=current.get("run_type"),
            mode=current.get("mode"),
            query=str(current.get("query") or "")[:1000],
            status=str(current.get("status") or "unknown"),
            started_at=_iso(current.get("started_at")),
            ended_at=_iso(current.get("ended_at")) if current.get("ended_at") else None,
            latency_ms=_ms_between(current.get("started_at"), current.get("ended_at")),
            current_node=current.get("current_node"),
            stage_count=len(stages),
            minor_count=sum(
                1 for s in stages if _minor_of(minors, int(s.get("stage_id") or 0))
            ),
            answer_len=len(answer),
        ),
        chain=chain_runs if chain else [
            r for r in chain_runs if r.run_id == current.get("run_id")
        ],
        verdicts=verdicts,
        timeline=timeline,
        evidence=evidence,
        context=context,
        performance=performance,
        anomalies=anomalies,
    )


def build_audit_detail(
    view: AuditView,
    stage_id: int,
    stages: list[dict],
    minors: Any,
    spans: list[dict],
    include_raw: bool = False,
) -> AuditNodeDetail:
    """单节点详情; raw 只在 include_raw=True 时填。"""
    nodes = {n.stage_id: n for n in build_nodes(stages, minors)}
    node = nodes.get(stage_id)
    if node is None:
        raise KeyError(f"stage {stage_id} 不存在")
    stage = next(s for s in stages if int(s.get("stage_id") or 0) == stage_id)
    minor = _minor_of(minors, stage_id) or {}
    output = _as_dict(minor.get("output_json"))
    decision_data = _as_dict(output.get("decision"))

    board = build_evidence(
        {"run_id": node.run_id, "mode": view.run.mode},
        stages,
        minors,
        spans,
    )
    node_calls = [c for c in board.tool_calls if c.stage_id == stage_id]
    node_docs = [d for d in board.documents if d.source_stage_id == stage_id]
    if node_docs:
        ids = {d.evidence_id for d in node_docs}
        node_gaps = [
            g for g in board.gaps
            if g.evidence_id in ids or g.stage_id == stage_id
        ]
        node_evidence = EvidenceBoard(
            tool_calls=node_calls,
            documents=node_docs,
            citations=[],
            gaps=node_gaps,
        )
    elif node.node_name in ANSWER_NODES:
        node_evidence = EvidenceBoard(
            tool_calls=node_calls,
            documents=[],
            citations=board.citations,
            gaps=[g for g in board.gaps if g.code == "cited_without_source"],
        )
    else:
        node_evidence = EvidenceBoard(
            tool_calls=node_calls,
            documents=[],
            citations=[],
            gaps=[g for g in board.gaps if g.stage_id == stage_id],
        )

    context_node = next(
        (c for c in view.context.nodes if c.stage_id == stage_id),
        ContextNode(stage_id=stage_id, node_name=node.node_name),
    )

    calls: list[CallItem] = []
    for span in spans or []:
        if _stage_for_span(span, stages) != stage_id and not (
            span.get("span_type") == "tool" and span.get("name") in (
                output.get("tool"), stage.get("node_name")
            )
        ):
            continue
        prompt, completion = _token_tuple(span.get("token_usage"))
        calls.append(
            CallItem(
                span_id=int(span.get("span_id") or span.get("id") or 0) or None,
                span_type=str(span.get("span_type") or ""),
                name=str(span.get("name") or ""),
                status=span.get("status"),
                latency_ms=span.get("latency_ms"),
                tokens={"prompt": prompt, "completion": completion},
            )
        )

    raw = None
    if include_raw:
        span = None
        for candidate in reversed(spans or []):
            if candidate.get("name") == stage.get("node_name"):
                span = candidate
                break
        raw = RawBlock(
            minor_state={
                "stage_id": stage_id,
                "node_name": stage.get("node_name"),
                "input_text": minor.get("input_text"),
                "output_text": minor.get("output_text"),
                "output_json": output,
                "context_meta": _as_dict(minor.get("context_meta")),
                "metrics": _as_dict(minor.get("metrics")),
                "refs": _as_dict(minor.get("refs")),
                "error": minor.get("error"),
            },
            span_input=(span or {}).get("input"),
            span_output=(span or {}).get("output"),
            span_state=(span or {}).get("state"),
        )

    return AuditNodeDetail(
        node=node,
        decision=DecisionDetail(
            summary=str(decision_data.get("summary") or node.decision_summary),
            reason=str(decision_data.get("reason") or minor.get("route_reason") or ""),
            steps=list(output.get("steps") or []),
            tool=output.get("tool"),
            args_digest=digest_value(output.get("args") or {}),
            hitl=str(output.get("hitl") or "") or None,
        ),
        evidence=node_evidence,
        context=context_node,
        calls=calls,
        raw=raw,
    )
