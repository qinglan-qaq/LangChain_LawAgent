"""
lawApp_LangGraph 统一数据模型 (v2 — LangGraph 1.x)

三层 Pydantic 模型体系：
A. 工具返回层 — RetrievedDocument / EvaluationResult / LawsResult / WebSearchResult
B. 计划执行层 — PlanStep / ToolCallRecord
C. 顶层 — AgentState(Plan & Execute Agent 状态)

v2 变更 (upgrade-v1)：
- 累积型字段使用 Annotated + reducer（append_list / add_messages），
  节点只返回增量更新 dict，禁止原地变异 state 对象
- PlanStep.status 收紧为 Literal 枚举
- 修正拼写: 检索评估字段名统一为 evaluate_retrieved_documents(旧拼写已全仓清除)
- 新增 HITL 相关字段（clarification / hitl_event），配合 interrupt() 使用
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Callable, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field


#  State Reducers — LangGraph 字段级合并策略
#
# append_list: 增量累加；节点返回 RESET 标记则清空（新一轮请求由 ingest 节点触发），
#              从而在多轮对话（同 thread_id）下避免上一轮的检索结果泄漏到本轮。


# RESET 用普通字符串标记:checkpointer 会对节点的每条写入值做 msgpack 序列化,
# 自定义哨兵对象会抛 "Type is not msgpack serializable"(MemorySaver/PostgresSaver 同一套 serde)。
# 这些字段的正常节点更新恒为列表,字符串标记不与之冲突。
RESET = "__lawapp_reset__"


def append_list(left: Optional[List] = None, right: Any = None) -> List:
    """Reducer：right 为增量列表则追加；为 RESET 标记则清空。"""
    if right == RESET:
        return []
    if right is None:
        return list(left or [])
    return list(left or []) + list(right)


#  messages 字段沿用 langgraph 官方 reducer（按 id 合并/删除消息）
try:  # langgraph >= 1.0
    from langgraph.graph.message import add_messages
except ImportError:  # pragma: no cover — 兼容旧版
    from langgraph.graph import add_messages  # type: ignore


#  A. 工具返回层 — Tool Output Models


class RetrievedDocument(BaseModel):
    """单条检索到的法律案例文档块"""

    rank: int = 0
    id: str = ""
    hybrid_score: float = 0.0
    year: str = ""
    case_number: str = ""
    case_cause: str = ""
    chunk_text: str = ""


class simpleRetrievedDocument(BaseModel):
    """单条检索到的法律案例文档块（供分析上下文使用的精简版）"""

    id: str = ""
    year: str = ""
    case_number: str = ""
    case_cause: str = ""
    chunk_text: str = ""


class LawsResult(BaseModel):
    """fetch_laws 工具返回的法律条文结果"""

    law_title: str = ""
    chapter: str = ""
    article_number: str = ""
    content: str = ""


class WebSearchResult(BaseModel):
    """单条网络检索结果"""

    title: str = ""
    link: str = ""
    snippet: str = ""


class EvaluationResult(BaseModel):
    """evaluate_case_relevance 工具返回 — CRAG 三档评估"""

    total: int = 0
    correct_count: int = 0
    ambiguous_count: int = 0
    incorrect_count: int = 0
    quality_verdict: str = ""
    correct: List[simpleRetrievedDocument] = Field(default_factory=list)
    ambiguous: List[simpleRetrievedDocument] = Field(default_factory=list)
    incorrect: List[simpleRetrievedDocument] = Field(default_factory=list)
    error: Optional[str] = None


class PromptsRecord(BaseModel):
    """记录最终回答的提示词内容"""

    query: str = ""
    web_search_results: List[WebSearchResult] = Field(default_factory=list)
    # 评估后的高质量案例（correct + ambiguous 档）
    evaluate_retrieved_documents: List[simpleRetrievedDocument] = Field(
        default_factory=list
    )
    laws_results: List[LawsResult] = Field(default_factory=list)


#  B. 计划执行层 — Plan & Execute Models

StepStatus = Literal["pending", "doing", "done", "failed"]


class PlanStep(BaseModel):
    """计划中的单个步骤"""

    step_id: int
    description: str
    tool_name: Optional[str] = None
    status: StepStatus = "pending"
    retry_count: int = 0


class ToolCallRecord(BaseModel):
    """单次工具调用的记录"""

    step_id: int
    tool_name: str
    tool_input: Dict[str, Any] = Field(default_factory=dict)
    output: Any = None
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


#  C. 顶层 — AgentState(Plan & Execute Agent)


class AgentState(BaseModel):
    """Plan & Execute Agent 的全局状态

    合并策略:
    - 无注解字段: 覆盖语义（节点返回完整新值）
    - append_list: 增量追加（web_search_results / law_results / tool_calls / reasoning）
    - add_messages: 按 id 合并消息（messages）
    """

    model_config = {"arbitrary_types_allowed": True}

    # 会话标识
    session_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    user_id: Optional[str] = None

    # 当前请求
    query: str = ""
    # 是否输出为 PDF
    is_pdf_output: bool = False

    # 对话历史（多轮，跨请求保留）
    messages: Annotated[List[Any], add_messages] = Field(default_factory=list)

    # 计划与执行（覆盖语义：各节点返回完整新列表）
    plan: List[PlanStep] = Field(default_factory=list)
    current_step_index: int = 0
    replan_needed: bool = False
    replan_reason: Optional[str] = None

    # ============= HITL（人机协同）=============
    # 反问轮次计数（每轮请求最多反问一次）
    clarification_round: int = 0
    # 反问内容与用户补充（{"question": str, "answer": str}）
    clarification: Optional[Dict[str, Any]] = None
    # 用户已确认高风险话题 / PDF 生成
    risk_confirmed: bool = False
    pdf_confirmed: bool = False
    # 最近一次 interrupt 事件（类型 / 载荷 / 时间），用于审计与前端展示（覆盖语义）
    hitl_event: Optional[Dict[str, Any]] = None

    # =============Agent显式输出结果=============

    final_answer: str = ""
    # 最终回答的提示词（包含所有上下文信息、rag资料、工具结果等）
    final_prompts: str = ""
    # 结构化提示词记录（评估结果 + 网络检索 + 法条检索）
    prompts_record: PromptsRecord = Field(default_factory=PromptsRecord)
    # 工具调用跟踪（增量追加）
    tool_calls: Annotated[List[ToolCallRecord], append_list] = Field(
        default_factory=list
    )
    # 思考链（增量追加）
    reasoning: Annotated[List[str], append_list] = Field(default_factory=list)

    # =============以下为工具返回结果==============

    # RAG 检索结果（覆盖：每次检索替换旧结果）
    rag_documents: List[RetrievedDocument] = Field(default_factory=list)
    # 评估结果（覆盖）
    evaluation: EvaluationResult = Field(default_factory=EvaluationResult)
    # 网络检索结果（增量追加）
    web_search_results: Annotated[List[WebSearchResult], append_list] = Field(
        default_factory=list
    )
    # 拼装后的 CRAG 上下文（覆盖）
    crag_context: EvaluationResult = Field(default_factory=EvaluationResult)
    # 长期记忆检索结果（覆盖）
    memory_results: List[Dict[str, Any]] = Field(default_factory=list)
    # 法律条文检索结果（增量追加）
    law_results: Annotated[List[LawsResult], append_list] = Field(
        default_factory=list
    )
    # 长期记忆写入确认（覆盖）
    memory_update: Optional[Dict[str, Any]] = None

    # =============以下为路由控制判断=============

    is_law_questions: bool = False
    is_simple_questions: bool = False
    pdf_path: Optional[str] = None

    # 流程控制
    should_continue: bool = True
    error: Optional[str] = None
