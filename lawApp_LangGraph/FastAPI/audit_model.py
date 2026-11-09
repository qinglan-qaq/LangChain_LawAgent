"""统一审计视图契约(2026-11-08 spec).

事实层仍是 trace_runs/stage_chain/minor_states/trace_spans; 本模块只描述
`audit_view.py` 派生出的只读模型, 供 API 响应与前端消费。字段命名与
docs/superpowers/specs/2026-11-08-monitor-unified-audit-design.md 对齐。
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

VerdictLevel = Literal["good", "warn", "bad", "unknown", "na"]
GroupKind = Literal["decision", "step", "hitl", "answer"]
EvidenceKind = Literal["law", "case", "memory", "web"]


class RunHeader(BaseModel):
    """审计对象 run 的头部信息。"""

    run_id: str
    session_id: Optional[str] = None
    parent_run_id: Optional[str] = None
    root_run_id: Optional[str] = None
    run_type: Optional[str] = None
    mode: Optional[str] = None
    query: Optional[str] = None
    status: str = "unknown"
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    latency_ms: int = 0
    current_node: Optional[str] = None
    stage_count: int = 0
    minor_count: int = 0
    answer_len: int = 0


class ChainRun(BaseModel):
    """HITL/resume 链中的单个 run。"""

    run_id: str
    parent_run_id: Optional[str] = None
    status: str = "unknown"
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    latency_ms: int = 0
    wait_before_ms: int = 0
    stage_count: int = 0
    has_audit_content: bool = False


class JumpTarget(BaseModel):
    """结论卡跳转目标。"""

    section: str = "timeline"
    stage_id: Optional[int] = None
    evidence_id: Optional[str] = None


class Verdict(BaseModel):
    """一张审计结论卡。"""

    key: str
    level: VerdictLevel = "unknown"
    summary: str = ""
    reasons: List[str] = Field(default_factory=list)
    jump: JumpTarget = Field(default_factory=JumpTarget)


class AuditVerdicts(BaseModel):
    """四张固定结论卡: 流程/证据/上下文/性能。"""

    flow: Verdict = Field(default_factory=lambda: Verdict(key="flow"))
    evidence: Verdict = Field(default_factory=lambda: Verdict(key="evidence"))
    context: Verdict = Field(default_factory=lambda: Verdict(key="context"))
    performance: Verdict = Field(
        default_factory=lambda: Verdict(key="performance")
    )


class TimelineGroup(BaseModel):
    """时间轴分组: 决策节点单列, 执行步骤按计划步骤折叠。"""

    group_id: str
    kind: GroupKind = "step"
    plan_round: int = 0
    step_index: Optional[int] = None
    label: str = ""
    stage_ids: List[int] = Field(default_factory=list)
    node_names: List[str] = Field(default_factory=list)
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    latency_ms: int = 0
    evidence_count: int = 0
    error_count: int = 0
    status: str = "ok"


class AuditNode(BaseModel):
    """单个 stage 的审计摘要(不含原文)。"""

    stage_id: int
    node_exec_id: str
    run_id: str
    node_name: str
    seq: int = 1
    plan_round: int = 0
    step_index: Optional[int] = None
    attempt: int = 1
    phase: str = "running"
    status: str = "running"
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    latency_ms: Optional[int] = None
    route: Optional[str] = None
    route_reason: Optional[str] = None
    decision_summary: str = ""
    evidence_count: int = 0
    citation_count: int = 0
    context_flags: List[str] = Field(default_factory=list)
    error: Optional[str] = None
    content_source: str = "projected"


class ToolCallEvidence(BaseModel):
    """一次工具调用(来自 trace_spans.tool)。"""

    span_id: Optional[int] = None
    stage_id: Optional[int] = None
    tool: str
    args_digest: str = ""
    status: Optional[str] = None
    latency_ms: Optional[int] = None
    counts: Dict[str, int] = Field(default_factory=dict)
    result_ref: str = ""


class DocumentEvidence(BaseModel):
    """一份检索/记忆/网络文档。"""

    evidence_id: str
    kind: EvidenceKind = "case"
    title: str = ""
    score: Optional[float] = None
    verdict: Optional[str] = None
    source_span_id: Optional[int] = None
    source_stage_id: Optional[int] = None
    used_in_analysis: Optional[bool] = None
    used_in_answer: Optional[bool] = None


class CitationEvidence(BaseModel):
    """终答引用及其证据关联。"""

    label: str
    type: str = "case"
    evidence_ids: List[str] = Field(default_factory=list)
    resolved: bool = False


class EvidenceGap(BaseModel):
    """证据链缺口。"""

    code: Literal[
        "retrieved_not_used", "cited_without_source", "no_evidence"
    ] = "no_evidence"
    label: str = ""
    stage_id: Optional[int] = None
    evidence_id: Optional[str] = None


class EvidenceBoard(BaseModel):
    """证据板: 工具调用 → 文档 → 引用 → 缺口。"""

    tool_calls: List[ToolCallEvidence] = Field(default_factory=list)
    documents: List[DocumentEvidence] = Field(default_factory=list)
    citations: List[CitationEvidence] = Field(default_factory=list)
    gaps: List[EvidenceGap] = Field(default_factory=list)
    truncated: bool = False


class ContextSegmentInfo(BaseModel):
    """一个上下文段在单节点的注入情况(只存 chars/hash, 不复制正文)。"""

    present: bool = False
    chars: int = 0
    hash: str = ""
    truncated: bool = False
    injected: bool = False


class ContextNode(BaseModel):
    """节点 × 上下文段。"""

    stage_id: int
    node_name: str
    segments: Dict[str, ContextSegmentInfo] = Field(default_factory=dict)
    diff: Dict[str, List[str]] = Field(default_factory=dict)
    inherited_from_stage_id: Optional[int] = None


class ContextSummary(BaseModel):
    """上下文矩阵汇总。"""

    total_chars: int = 0
    truncated_segments: List[str] = Field(default_factory=list)
    history_turns: Optional[int] = None
    memory_hits: int = 0


class ContextMatrix(BaseModel):
    """上下文矩阵。"""

    segments: List[str] = Field(default_factory=list)
    nodes: List[ContextNode] = Field(default_factory=list)
    summary: ContextSummary = Field(default_factory=ContextSummary)


class PerformanceSlow(BaseModel):
    """慢点排行条目。"""

    stage_id: int
    node_name: str
    latency_ms: int = 0
    pct_of_execution: float = 0.0


class PerformanceError(BaseModel):
    """错误/重试条目。"""

    stage_id: Optional[int] = None
    node_name: str = ""
    error: str = ""
    retry: int = 0


class TokenBoard(BaseModel):
    """token 汇总。"""

    total: int = 0
    prompt: int = 0
    completion: int = 0
    by_node: Dict[str, int] = Field(default_factory=dict)


class PerformanceBoard(BaseModel):
    """性能板。"""

    total_ms: int = 0
    execution_ms: int = 0
    wait_human_ms: int = 0
    overhead_ms: int = 0
    slowest: List[PerformanceSlow] = Field(default_factory=list)
    errors: List[PerformanceError] = Field(default_factory=list)
    tokens: TokenBoard = Field(default_factory=TokenBoard)
    retries: int = 0
    limit_hit: bool = False


class Anomaly(BaseModel):
    """固定 code 的异常/缺口条目。"""

    code: str
    severity: Literal["info", "warn", "error"] = "warn"
    title: str = ""
    detail: str = ""
    run_id: Optional[str] = None
    stage_id: Optional[int] = None
    evidence_id: Optional[str] = None


class AuditView(BaseModel):
    """一次提问全链的统一审计视图。"""

    schema_version: int = 1
    generated_at: str
    degraded: bool = False
    run: RunHeader
    chain: List[ChainRun] = Field(default_factory=list)
    verdicts: AuditVerdicts = Field(default_factory=AuditVerdicts)
    timeline: List[TimelineGroup] = Field(default_factory=list)
    evidence: EvidenceBoard = Field(default_factory=EvidenceBoard)
    context: ContextMatrix = Field(default_factory=ContextMatrix)
    performance: PerformanceBoard = Field(default_factory=PerformanceBoard)
    anomalies: List[Anomaly] = Field(default_factory=list)


class DecisionDetail(BaseModel):
    """节点决策 tab。"""

    summary: str = ""
    reason: str = ""
    steps: List[Dict[str, Any]] = Field(default_factory=list)
    tool: Optional[str] = None
    args_digest: Optional[str] = None
    hitl: Optional[str] = None


class CallItem(BaseModel):
    """节点下的 span 调用。"""

    span_id: Optional[int] = None
    span_type: str = ""
    name: str = ""
    status: Optional[str] = None
    latency_ms: Optional[int] = None
    tokens: Dict[str, int] = Field(default_factory=dict)


class RawBlock(BaseModel):
    """原始数据 tab(仅 include_raw=1)。"""

    minor_state: Dict[str, Any] = Field(default_factory=dict)
    span_input: Any = None
    span_output: Any = None
    span_state: Any = None


class AuditNodeDetail(BaseModel):
    """单节点审计详情。"""

    node: AuditNode
    decision: DecisionDetail = Field(default_factory=DecisionDetail)
    evidence: EvidenceBoard = Field(default_factory=EvidenceBoard)
    context: ContextNode
    calls: List[CallItem] = Field(default_factory=list)
    raw: Optional[RawBlock] = None
