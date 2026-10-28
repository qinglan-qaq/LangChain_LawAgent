from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

#  请求


class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=5000, description="用户问题")
    session_id: Optional[str] = Field(
        default=None, description="会话 ID,传入可持续多轮对话;不传则新建"
    )


class AttorneyAskRequest(BaseModel):
    """代理律师模式咨询请求。"""

    query: str = Field(..., min_length=1, max_length=5000, description="用户法律问题")
    session_id: Optional[str] = Field(default=None, description="续聊会话 ID")


class AssistantAskRequest(BaseModel):
    """律师助理模式文书起草请求(婚姻家事类)。"""

    case_details: str = Field(..., min_length=20, max_length=20000, description="完整案件详情")
    doc_type: Literal["complaint", "defense"] = Field(
        ..., description="complaint=起诉状, defense=答辩状"
    )
    session_id: Optional[str] = Field(default=None, description="续聊会话 ID")


class AssistantStreamRequest(BaseModel):
    """律师助理模式 SSE 流式请求(M11: 长案情走 POST body, 规避浏览器/代理
    URL 长度限制; 行为与 GET /assistant/ask/stream 完全一致)。"""

    case_details: str = Field(..., min_length=1, max_length=4000, description="完整案件详情")
    doc_type: Literal["complaint", "defense"] = Field(
        default="complaint", description="complaint=起诉状, defense=答辩状"
    )
    session_id: Optional[str] = Field(default=None, description="续聊会话 ID")


class ResumeRequest(BaseModel):
    """HITL resume:对 interrupt 的回复(反问补充 / 高风险确认 / PDF 确认)."""

    session_id: str = Field(..., description="发生 interrupt 的会话 ID")
    answer: str = Field(
        default="",
        max_length=3000,
        description="用户的回复内容;y/是 确认,n/否/跳过 拒绝",
    )
    parent_run_id: Optional[str] = Field(
        default=None,
        description="发生 interrupt 的上一 run_id; 用于监控时间轴串联",
    )


class FeedbackRequest(BaseModel):
    session_id: str = Field(..., description="被评价的会话 ID")
    rating: int = Field(..., ge=1, le=5, description="1-5 星")
    comment: str = Field(default="", max_length=2000, description="可选评价内容")


#  响应


class ToolInfo(BaseModel):
    """工具元信息"""

    name: str
    description: str


class SourceInfo(BaseModel):
    """回答引用的来源"""

    case_number: str = ""
    year: str = ""
    snippet: str = ""
    title: str = ""
    link: str = ""


class QueryResponse(BaseModel):
    query: str
    session_id: str
    final_answer: str
    final_prompt: str = ""
    sources: List[SourceInfo] = Field(default_factory=list)
    tool_calls: List[str] = Field(default_factory=list)
    reasoning: List[str] = Field(default_factory=list)
    # 中间 interrupt 请求(未完成时非空):type=clarify/risk_confirm/pdf_confirm
    interrupt: Optional[Dict[str, Any]] = None
    # 结构化提示词记录(评估 + 网络检索 + 法条)
    prompts_record: Dict[str, Any] = Field(default_factory=dict)
    # 案件要素面板数据(子项目A 澄清循环)
    elements: List[Dict[str, Any]] = Field(default_factory=list)
    # 澄清历史(需求3 后端侧): 每轮反问/回答/推荐选项记录
    clarify_history: Optional[List[Dict[str, Any]]] = None
    # 工具使用 JSON 记录: {tool_name: [结果摘要, ...]}(用户决策 v4)
    tool_usage: Dict[str, List[Any]] = Field(default_factory=dict)
    # Word 文书产物路径(assistant 模式 docx 确认生成后非空)
    docx_path: Optional[str] = None


#  监控页响应(D-spec §五, /monitor 4 端点契约)


class MonitorStage(BaseModel):
    """拉链表 stage_chain 单行(节点一次执行)。"""

    node_name: str
    seq: int
    status: str  # running|ok|error|interrupted|cancelled
    started_at: str
    ended_at: Optional[str] = None
    latency_ms: Optional[int] = None
    detail: Dict[str, Any] = Field(default_factory=dict)


class MonitorSpan(BaseModel):
    """trace_spans 单行(节点/工具/LLM 全量明细, 事后查看)。"""

    span_type: str
    name: str
    status: Optional[str] = None
    input: Any = None
    output: Any = None
    state: Any = None
    latency_ms: Optional[int] = None
    token_usage: Any = None
    started_at: Optional[str] = None


class MonitorRunItem(BaseModel):
    """runs 列表行 — 拉链表聚合的 stage 三计数进此层。"""

    run_id: str
    session_id: Optional[str] = None
    run_type: str
    mode: Optional[str] = None
    status: str
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    stage_total: int = 0
    stage_ok: int = 0
    stage_running: int = 0
    metrics: Dict[str, Any] = Field(default_factory=dict)
    # 2026-10-23 minorStates: run 串联与内容就绪度
    parent_run_id: Optional[str] = None
    current_node: Optional[str] = None
    minor_total: int = 0
    content_available: bool = False
    stale: bool = False


class MonitorRunDetail(MonitorRunItem):
    """run 详情 = 列表行 + 拉链 stages + spans 全量。"""

    stages: List[MonitorStage] = Field(default_factory=list)
    spans: List[MonitorSpan] = Field(default_factory=list)


class MonitorOverview(BaseModel):
    """总览卡: 24h run 状态分布 / 实时开行数 / 限次命中 / 节点失败 Top / 分数分布。"""

    runs_by_status: Dict[str, int] = Field(default_factory=dict)
    running_stages: int = 0
    limit_hit_runs: int = 0
    node_fail_top: List[Dict[str, Any]] = Field(default_factory=list)
    score_distribution: Dict[str, int] = Field(default_factory=dict)
    running_minor_nodes: int = 0
    minor_fail_top: List[Dict[str, Any]] = Field(default_factory=list)


class MonitorEval(BaseModel):
    """评测批次行(eval_runs: P2 golden set 批跑指标)。"""

    id: int
    dataset: str
    label: str
    metrics: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None


#  minorStates 内容层(v2) — 节点级时间轴与详情


class MinorState(BaseModel):
    """minor_states 1:1 stage_chain 内容行(缺失时由 API 合成占位)。"""

    stage_id: int
    node_exec_id: str
    node_name: str
    seq: int
    attempt: int = 1
    phase: str = "running"  # running|waiting_human|done
    status: str = "running"
    success: Optional[bool] = None
    started_at: str
    ended_at: Optional[str] = None
    latency_ms: Optional[int] = None
    input_text: Optional[str] = None
    output_text: Optional[str] = None
    output_json: Dict[str, Any] = Field(default_factory=dict)
    context_meta: Dict[str, Any] = Field(default_factory=dict)
    metrics: Dict[str, Any] = Field(default_factory=dict)
    route: Optional[str] = None
    route_reason: Optional[str] = None
    error: Optional[str] = None
    refs: Dict[str, Any] = Field(default_factory=dict)
    parent_stage_id: Optional[int] = None
    content_source: str = "projected"


class TimelineItem(BaseModel):
    """时间轴条目: 节点卡或等待人工分隔条。"""

    kind: str  # node | divider
    run_id: str
    state: Optional[MinorState] = None
    text: Optional[str] = None
    percent_of_run: Optional[float] = None
    percent_of_execution: Optional[float] = None


class TimelineComposition(BaseModel):
    """单 run 时长构成(毫秒 + 百分比)。"""

    wall_clock_ms: int = 0
    execution_ms: int = 0
    wait_human_ms: int = 0
    overhead_ms: int = 0
    execution_pct: float = 0.0
    wait_pct: float = 0.0
    overhead_pct: float = 0.0


class TimelineRun(BaseModel):
    """链视图中的单个 run。"""

    run_id: str
    parent_run_id: Optional[str] = None
    status: str
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    composition: TimelineComposition = Field(default_factory=TimelineComposition)


class MonitorTimeline(BaseModel):
    """时间轴响应: 链头 run + 链 runs + 节点/分隔条目 + 链聚合构成。"""

    run_id: str
    runs: List[TimelineRun] = Field(default_factory=list)
    items: List[TimelineItem] = Field(default_factory=list)
    total_composition: Optional[TimelineComposition] = None


class MonitorConfig(BaseModel):
    """前端阈值/文本上限(标定值下发, 避免两处硬编码)。"""

    correct_threshold: float
    incorrect_threshold: float
    minor_text_limit: int


class MonitorStageDetail(BaseModel):
    """单阶段详情: minor 行 + 按需展开的 span 原文/完整回答。"""

    state: MinorState
    span: Optional[MonitorSpan] = None
    final_answer: Optional[str] = None
    refs_resolved: Dict[str, Any] = Field(default_factory=dict)
