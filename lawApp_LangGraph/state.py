"""
lawApp_LangGraph 统一数据模型 (v2 — LangGraph 1.x)

三层 Pydantic 模型体系:
A. 工具返回层 — RetrievedDocument / EvaluationResult / LawsResult / WebSearchResult
B. 计划执行层 — PlanStep / ToolCallRecord
C. 顶层 — AgentState(Plan & Execute Agent 状态)

v2 变更 (upgrade-v1):
- 累积型字段使用 Annotated + reducer(append_list / add_messages)，
  节点只返回增量更新 dict，禁止原地变异 state 对象
- PlanStep.status 收紧为 Literal 枚举
- 修正拼写: 检索评估字段名统一为 evaluate_retrieved_documents(旧拼写已全仓清除)
- HITL 重构为要素驱动澄清循环:CaseElements 要素清单 + clarify_history
  澄清历史 + AgentState 澄清循环字段,配合 interrupt() 使用
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

#  State Reducers — LangGraph 字段级合并策略
#
# append_list: 增量累加；节点返回 RESET 标记则清空(新一轮请求由 ingest 节点触发)，
#              从而在多轮对话(同 thread_id)下避免上一轮的检索结果泄漏到本轮.


# RESET 用普通字符串标记:checkpointer 会对节点的每条写入值做 msgpack 序列化,
# 自定义哨兵对象会抛 "Type is not msgpack serializable"(MemorySaver/PostgresSaver 同一套 serde).
# 这些字段的正常节点更新恒为列表,字符串标记不与之冲突.
RESET = "__lawapp_reset__"


def append_list(left: Optional[List] = None, right: Any = None) -> List:
    """Reducer:right 为增量列表则追加；为 RESET 标记则清空."""
    if right == RESET:
        return []
    if right is None:
        return list(left or [])
    return list(left or []) + list(right)


#  messages 字段沿用 langgraph 官方 reducer(按 id 合并/删除消息)
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
    """单条检索到的法律案例文档块(供分析上下文使用的精简版)"""

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
    # 评估后的高质量案例(correct + ambiguous 档)
    evaluate_retrieved_documents: List[simpleRetrievedDocument] = Field(
        default_factory=list
    )
    laws_results: List[LawsResult] = Field(default_factory=list)
    # 已知案件要素 digest(由 merge 节点填充, 分析上下文注入)
    known_elements: str = ""


#  案件要素清单 — 子项目A 澄清循环的数据基础
#  轮数上限/连败阈值见 config.py(settings.max_clarify_rounds / error_streak_threshold)

# 默认要素清单:(key, label, 关键级) — 婚姻家事与语料库领域对齐
_DEFAULT_ELEMENTS: tuple[tuple[str, str, bool], ...] = (
    ("marriage_status", "婚姻现状", True),
    ("demand", "核心诉求", True),
    ("property", "主要财产与归属", True),
    ("children", "子女情况", False),
    ("timeline", "关键时间线", False),
    ("evidence", "手头证据", False),
    ("opposing_stance", "对方态度", False),
)


class CaseElement(BaseModel):
    """单个案件要素的状态.

    澄清循环的最小单元:key 唯一标识要素,critical 标记是否必须补齐,
    status / value / updated_by 共同记录要素级状态机的当前状态.
    """

    key: str
    label: str
    critical: bool
    status: Literal["known", "missing", "na"] = "missing"
    value: str = ""
    updated_by: str = "init"  # init / assess / ask / mid_clarify


class ElementQuestion(BaseModel):
    """评估 LLM 生成的单个要素反问.

    由评估节点写入 pending_questions,反问节点读取后转为 interrupt
    载荷;随 state 序列化,支撑 checkpoint 恢复.
    """

    key: str
    question: str


class CaseElements(BaseModel):
    """案件要素清单 — 澄清循环的状态机载体.

    elements 为全量要素快照(覆盖语义写入);要素级查询与状态转移
    均经由本类方法完成,是 ingest 重建与评估更新的共同载体.
    """

    elements: List[CaseElement] = Field(default_factory=list)

    def digest(self) -> str:
        """拼装已知要素的提示词注入文本.

        仅纳入 status 为 known 且取值非空的要素,以「标签:摘要」
        竖线拼接;无已知要素时返回占位文本.

        Returns:
            str: 已知要素的注入文本,如「婚姻现状:在婚 | 核心诉求:离婚」.
        """
        known = [e for e in self.elements if e.status == "known" and e.value]
        if not known:
            return "暂无已知要素"
        return " | ".join(f"{e.label}:{e.value}" for e in known)

    def critical_missing(self) -> List[CaseElement]:
        """返回仍缺失的关键要素列表.

        Returns:
            List[CaseElement]: critical 为真且 status 为 missing 的要素,
            是反问选择与降级判断的输入.
        """
        return [e for e in self.elements if e.critical and e.status == "missing"]

    def mark_na(self, keys: List[str]) -> None:
        """将指定要素标记为不适用(na).

        Args:
            keys (List[str]): 要标记为不适用的要素 key 列表.
        """
        for e in self.elements:
            if e.key in keys:
                e.status = "na"

    def promote(self, keys: List[str]) -> None:
        """将指定要素升级为关键要素.

        Args:
            keys (List[str]): 要升为关键的要素 key 列表.
        """
        for e in self.elements:
            if e.key in keys:
                e.critical = True

    def update(self, key: str, value: str, by: str) -> None:
        """写入单个要素的已知取值并置为 known.

        命中 key 的要素记录取值与更新来源;未命中则静默忽略
        (清单 key 由 default_case_elements 固定).

        Args:
            key (str): 要素唯一标识.
            value (str): 要素取值(自然语言摘要).
            by (str): 更新来源标记,如 init / assess / ask / mid_clarify.
        """
        for e in self.elements:
            if e.key == key:
                e.status = "known"
                e.value = value
                e.updated_by = by
                return


def default_case_elements() -> CaseElements:
    """按默认清单构建案件要素快照.

    供 ingest 节点在每轮请求开始时重建 case_elements
    (覆盖语义字段的默认工厂).

    Returns:
        CaseElements: 含 7 个默认要素的清单实例.
    """
    return CaseElements(
        elements=[
            CaseElement(key=k, label=lbl, critical=crit)
            for k, lbl, crit in _DEFAULT_ELEMENTS
        ]
    )


class ClarifyExchange(BaseModel):
    """一轮澄清交互的记录.

    审计与 B 子项目记忆固化的原料:round 记录轮次,element_keys
    标记该轮涉及的要素,at 记录交互时间.
    """

    round: int
    question: str
    answer: str
    element_keys: List[str]
    at: str = Field(default_factory=lambda: datetime.now().isoformat())


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
    - 无注解字段: 覆盖语义(节点返回完整新值)
    - append_list: 增量追加(web_search_results / law_results / tool_calls / reasoning)
    - add_messages: 按 id 合并消息(messages)
    """

    model_config = {"arbitrary_types_allowed": True}

    # 当前请求
    query: str = ""
    mode: str = "attorney"  # attorney=代理律师咨询 / assistant=律师助理文书起草
    doc_type: str = ""  # assistant 模式: complaint=起诉状 / defense=答辩状
    # 咨询分类(element_assess 写入): marriage_legal/concept/chitchat/other
    question_category: str = "marriage_legal"

    # 对话历史(多轮，跨请求保留)
    messages: Annotated[List[Any], add_messages] = Field(default_factory=list)

    # 计划与执行(覆盖语义:各节点返回完整新列表)
    plan: List[PlanStep] = Field(default_factory=list)
    current_step_index: int = 0
    replan_needed: bool = False
    replan_reason: Optional[str] = None
    # 不足原因诊断(vague/not_found/error/none) — replan_check 写入,路由读取
    insufficient_reason: str = "none"

    #  HITL(人机协同)
    # 案件要素清单(覆盖语义, ingest 重建默认清单)
    case_elements: CaseElements = Field(default_factory=default_case_elements)
    # 澄清历史(增量追加, ingest RESET 清空)
    clarify_history: Annotated[List[ClarifyExchange], append_list] = Field(
        default_factory=list
    )
    # 评估节点写入、反问节点读取(覆盖语义)
    pending_questions: List[ElementQuestion] = Field(default_factory=list)
    # 澄清轮数(上限 settings.max_clarify_rounds, ingest 归零)
    clarify_rounds: int = 0
    # 用户已确认高风险话题 / PDF 生成
    risk_confirmed: bool = False
    pdf_confirmed: bool = False
    # 工具连续失败计数(成功清零, >=settings.error_streak_threshold 触发降级)
    error_streak: int = 0
    # 一次性标记(防循环, ingest 重置)
    mid_clarify_used: bool = False
    budget_hitl_used: bool = False
    degrade_used: bool = False
    # 最近一次 interrupt 事件(覆盖语义)
    hitl_event: Optional[Dict[str, Any]] = None

    # Agent显式输出结果

    final_answer: str = ""
    # 最终回答的提示词(包含所有上下文信息、rag资料、工具结果等)
    final_prompts: str = ""
    # 结构化提示词记录(评估结果 + 网络检索 + 法条检索)
    prompts_record: PromptsRecord = Field(default_factory=PromptsRecord)
    # 工具调用跟踪(增量追加)
    tool_calls: Annotated[List[ToolCallRecord], append_list] = Field(
        default_factory=list
    )
    # 思考链(增量追加)
    reasoning: Annotated[List[str], append_list] = Field(default_factory=list)

    # 以下为工具返回结果=

    # RAG 检索结果(覆盖:每次检索替换旧结果)
    rag_documents: List[RetrievedDocument] = Field(default_factory=list)
    # 评估结果(覆盖)
    evaluation: EvaluationResult = Field(default_factory=EvaluationResult)
    # 网络检索结果(增量追加)
    web_search_results: Annotated[List[WebSearchResult], append_list] = Field(
        default_factory=list
    )
    # 法律条文检索结果(增量追加)
    law_results: Annotated[List[LawsResult], append_list] = Field(default_factory=list)

    # 以下为路由控制判断

    pdf_path: Optional[str] = None
    error: Optional[str] = None
