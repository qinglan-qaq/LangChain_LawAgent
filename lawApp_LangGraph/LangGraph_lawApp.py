"""
Plan & Execute Agent v2 — 法律咨询智能体 (LangGraph 1.x)

双 LLM 架构:
    llm_planner  (DeepSeek Pro)   → structured output 制定计划/重规划
    llm_executor (DeepSeek Flash) → bind_tools 逐步执行、调用工具

Graph 流程 (15 节点):
    START → ingest → risk_gate(HITL-1 高风险确认) → element_assess
    element_assess ──[chitchat 闲聊类]──→ chitchat(轻量应答) → END
    element_assess ⇄ ask_element(HITL-2 要素反问, 最多 settings.max_clarify_rounds 轮)
    element_assess ──[要素齐/轮数尽]──→ planner
    planner ──[plan 空]──→ finalize → END
    planner ──[有步骤]──→ executor ⇄ tools(ToolNode) → merge
    executor/merge ──[连续失败≥阈值]──→ hitl_degrade(HITL-4)
        hitl_degrade ──[retry→replanner | skip→replan_check | abort→finalize]
    executor/merge ──[步骤完成]──→ replan_check
        replan_check ──[质量通过]──→ finalize
        replan_check ──[预算耗尽]──→ hitl_budget(HITL-6)
            hitl_budget ──[补充→replanner | 收尾→finalize]
        replan_check ──[vague 未问过]──→ mid_clarify(HITL-5) → replanner
        replan_check ──[其余]──→ replanner → executor

v2 变更 (upgrade-v1):
- 4 处「剥代码栅栏 + json.loads」全部改为 with_structured_output(Pydantic Schema)
- executor 手动 TOOL_BY_NAME 循环 → prebuilt ToolNode + bind_tools(单工具)
- 全节点 async(replan_check / replanner 由同步 .invoke 改为 await ainvoke）
- 删除 _TOOL_FALLBACK_ARGS 字典(structured 单次重试取代）
- 新增 ingest 节点:每轮请求重置累积字段(reducer + RESET 标记）,
多轮对话不再泄漏上一轮的检索/调用记录
- HITL 六处 interrupt() + Command(resume=...):
    (1) risk_gate: 高风险话题确认(拒绝 → 热线文案中止)
    (2) ask_element: 关键要素缺失反问(要素循环, 最多 settings.max_clarify_rounds 轮)
    (3) executor: markdown_to_pdf 执行前确认
    (4) hitl_degrade: 工具连续失败降级询问(重试/跳过/终止)
    (5) mid_clarify: 检索反馈追问(先问人后搜网)
    (6) hitl_budget: 重规划预算耗尽询问(补充/收尾)
- LLM 懒加载单例,模块导入不再要求 API Key(可安全冒烟测试）
- 持久化: checkpointer(PostgresSaver/MemorySaver) + store(PostgresStore/InMemoryStore)
由 FastAPI lifespan 注入；记忆工具经 langgraph.config.get_store() 访问
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import interrupt

from lawApp_LangGraph.FastAPI.logging import debug
from lawApp_LangGraph.state import (
    RESET,
    AgentState,
    CaseElement,
    CaseElements,
    ClarifyExchange,
    ElementQuestion,
    EvaluationResult,
    PlanStep,
    PromptsRecord,
    ToolCallRecord,
    default_case_elements,
)
from lawApp_LangGraph import dialogue_log  # 方案c: JSON 会话历史事件流落 PG
from lawApp_LangGraph.tools import ALL_TOOLS
from lawApp_LangGraph.FastAPI.utils import is_command_word

load_dotenv()

from lawApp_LangGraph.config import settings

#  Task 3: 提示词统一改用 prompts.py 单一来源(旧版常量已于 Task 6 删除).
from lawApp_LangGraph.prompts import (
    BUDGET_CONFIRM_MSG,
    CHITCHAT_PROMPT,
    DEGRADE_CONFIRM_MSG,
    ELEMENT_ASSESS_PROMPT,
    EXECUTOR_PROMPT,
    FINALIZE_CASE_PROMPT,
    FINALIZE_DIRECT_PROMPT,
    MID_CLARIFY_PROMPT,
    PLANNER_SYSTEM,
    REPLAN_CHECK_PROMPT,
    REPLANNER_SYSTEM_PROMPT,
    RISK_GATE_PROMPT,
    SEMANTIC_CONFIRM_PROMPT,
)
from lawApp_LangGraph.tools.rag_tools import analyze_legal_issue  # noqa — 已有,确认不缺
from lawApp_LangGraph.tracing import get_instrumented_llm_cls, traced

#  LLM 懒加载单例 — 导入期不触碰 API Key; Lock 双检防多线程/多 worker 重复初始化

_llm_planner = None
_llm_planner_lock = threading.Lock()
_llm_executor = None
_llm_executor_lock = threading.Lock()


def get_planner_llm():
    """Pro LLM(规划/重规划,强推理）。测试可 monkeypatch 本函数。"""
    global _llm_planner
    if _llm_planner is None:
        with _llm_planner_lock:
            if _llm_planner is None:
                # 观测包装(决策 6): 拦截 _agenerate/_astream 记 llm span;
                # 子类 → _structured 的 isinstance(ChatOpenAI) 分支保持生效
                _llm_planner = get_instrumented_llm_cls()(
                    model=settings.deepseek_pro_model,
                    temperature=0.4,
                    max_tokens=4096,
                    openai_api_key=settings.deepseek_api_key,
                    openai_api_base=settings.deepseek_base_url,
                )
    return _llm_planner


def get_executor_llm():
    """Flash LLM(执行/质量门控,低成本低延迟）。测试可 monkeypatch 本函数。"""
    global _llm_executor
    if _llm_executor is None:
        with _llm_executor_lock:
            if _llm_executor is None:
                _llm_executor = get_instrumented_llm_cls()(
                    model=settings.deepseek_flash_model,
                    temperature=0.25,
                    max_tokens=2048,
                    openai_api_key=settings.deepseek_api_key,
                    openai_api_base=settings.deepseek_base_url,
                )
    return _llm_executor


def _structured(schema):
    """Flash LLM 结构化输出链: 真实 ChatOpenAI 走 method="json_mode"
    (默认 json_schema 被 DeepSeek API 拒, HTTP 400); 冒烟测试替身的
    with_structured_output 只收位置参数, 按替身原签名调用。
    """
    llm = get_executor_llm()
    try:
        from langchain_openai import ChatOpenAI

        if isinstance(llm, ChatOpenAI):
            return llm.with_structured_output(schema, method="json_mode")
    except ImportError:
        pass
    return llm.with_structured_output(schema)


def _tool_by_name(name: str) -> Optional[Any]:
    """运行期按名查工具(每次现读 ALL_TOOLS 注册表)。

    取代导入期快照 TOOL_BY_NAME: MCP 工具在 runtime.setup_runtime() 里
    经 register_mcp_tools 注入, 晚于本模块导入;快照会导致 planner 规划出的
    MCP 工具名在 _normalize_plan 被剥成 None、executor 裸下标 KeyError(H1)。
    """
    if not name:
        return None
    for t in ALL_TOOLS():
        if t.name == name:
            return t
    return None

# 工具返回 dict 中与 AgentState 同名的 key 将被 merge 节点合并
_STATE_KEYS = {
    "rag_documents",
    "evaluation",
    "final_answer",
    "final_prompts",
    "web_search_results",
    "pdf_path",
    "law_results",
    "prompts_record",
}


#  Structured Output Schemas — 取代「剥栅栏 + json.loads」


def _schema_models():
    from pydantic import BaseModel, Field

    class PlannedStepSchema(BaseModel):
        step_id: int = Field(default=1, description="步骤编号,从1开始")
        description: str = Field(default="", description="步骤的自然语言描述")
        tool_name: Optional[str] = Field(
            default=None, description="要执行的工具名;不需要工具则填 null"
        )

    class PlanSchema(BaseModel):
        reasoning: List[str] = Field(default_factory=list, description="思考过程")
        plan: List[PlannedStepSchema] = Field(
            default_factory=list, description="执行计划"
        )

    class ReplanCheckSchema(BaseModel):
        needs_replan: bool = Field(description="当前信息是否不足以生成高质量回答")
        reason: str = Field(default="", description="简短判断依据,不超过50字")
        insufficient_reason: Literal["vague", "not_found", "error", "none"] = Field(
            default="none",
            description="不足原因: vague=问题笼统(先问人) / not_found=案例库覆盖不到(联网) / error=执行错误 / none=不不足",
        )

    class RiskSchema(BaseModel):
        high_risk: bool = Field(description="是否命中高风险判定标准")
        reason: str = Field(default="", description="命中的标准,不超过30字")

    class ElementUpdate(BaseModel):
        key: str = Field(description="要素 key,必须是清单内的 key")
        value: str = Field(default="", description="从用户回答提取的要素摘要")
        status: Literal["known", "na"] = Field(default="known")

    class ElementAssessmentSchema(BaseModel):
        question_category: Literal["marriage_legal", "concept", "chitchat", "other"] = (
            Field(default="marriage_legal", description="咨询分类四选一")
        )
        applicable: bool = Field(description="是否婚姻家事类咨询")
        element_updates: List[ElementUpdate] = Field(
            default_factory=list, description="用户上轮回答映射到的要素"
        )
        na_keys: List[str] = Field(default_factory=list, description="本案不涉及的要素")
        promote_keys: List[str] = Field(
            default_factory=list, description="按案由升关键的要素"
        )
        questions: List[ElementQuestion] = Field(
            default_factory=list, description="本轮反问,每轮只生成 1 个,聚焦最关键的 missing 要素"
        )
        done: bool = Field(description="要素已足够,无需再问")

    class MidClarifySchema(BaseModel):
        question: str = Field(description="一个聚焦追问,律师问诊语气,一句话")
        element_key: str = Field(default="", description="追问对应的要素 key")
        options: List[str] = Field(
            default_factory=list, description="该追问的 2~3 个推荐选项,无合适选项时为空数组"
        )

    class ConfirmSchema(BaseModel):
        proceed: bool = Field(description="用户回复语义是否为同意继续")

    return (
        PlanSchema,
        ReplanCheckSchema,
        RiskSchema,
        ElementAssessmentSchema,
        MidClarifySchema,
        ConfirmSchema,
    )


(
    PlanSchema,
    ReplanCheckSchema,
    RiskSchema,
    ElementAssessmentSchema,
    MidClarifySchema,
    ConfirmSchema,
) = _schema_models()


# CoT 总线: thread_id → 订阅队列集合; SSE 端点开道, planner/replanner 推 reasoning 增量
# (H4: 值改 set[Queue] fan-out —— 同会话多订阅互不覆盖, 断开只摘自己的队列)
_REASONING_BUS: dict[str, "set[asyncio.Queue]"] = {}


def open_reasoning_channel(thread_id: str) -> "asyncio.Queue":
    """为会话开启 reasoning 通道;SSE 端点在 graph.astream 之前调用。

    Args:
        thread_id: 会话 ID(与 graph_config 的 configurable.thread_id 一致)。

    Returns:
        本次调用新建的独立 Queue(已加入订阅集); planner/replanner 经
        _bus_put 广播 {"source","delta"}, 结束由 SSE 端推 None 哨兵。
    """
    import asyncio

    q: asyncio.Queue = asyncio.Queue()
    _REASONING_BUS.setdefault(thread_id, set()).add(q)
    return q


def close_reasoning_channel(thread_id: str, q: "asyncio.Queue" = None) -> None:
    """关闭并移除 reasoning 通道(幂等)。

    q 给定时只摘该订阅队列(流断开不影响同会话其他订阅);
    不给 q 时整个会话通道移除(兼容旧调用)。
    """
    if q is None:
        _REASONING_BUS.pop(thread_id, None)
        return
    subs = _REASONING_BUS.get(thread_id)
    if subs is not None:
        subs.discard(q)
        if not subs:
            _REASONING_BUS.pop(thread_id, None)


def _bus_put(thread_id: str, item) -> None:
    """向会话全部 reasoning 队列广播一条事件(H4 fan-out)。

    put_nowait 非阻塞(队列无界不会满); 已关闭/死队列移除且吞异常,
    观测旁路失败不打穿节点执行。
    """
    subs = _REASONING_BUS.get(thread_id)
    if not subs:
        return
    for q in list(subs):
        try:
            q.put_nowait(item)
        except Exception:
            subs.discard(q)


#  工作状态上报 — 复用 reasoning 总线, source="status" 区分; SSE 端 pump 原样转发
_NODE_LABELS = {
    "element_assess": "评估案件要素",
    "chitchat": "生成闲聊回应",
    "planner": "规划检索策略",
    "replanner": "重规划补充步骤",
    "replan_check": "质量门控检查",
    "finalize": "组装最终回答",
    "merge": "合并工具结果",
    "mid_clarify": "生成聚焦追问",
}


async def _publish_status(config, text: str) -> None:
    """向会话的 SSE 通道推一条工作状态(前端"正在执行"指示)。

    节点入口/工具调用前调用;通道未开(非流式调用/测试)时静默跳过。
    """
    cfg = (config or {}).get("configurable") or {}
    _bus_put(cfg.get("thread_id", ""), {"source": "status", "delta": text})


async def _publish_node_status(config, node: str) -> None:
    """按节点名推标准化的"正在执行 X"状态。"""
    await _publish_status(config, f"正在{_NODE_LABELS.get(node, node)}")


#  方案c: 会话历史事件流的 session_id 来源 —— LangGraph thread_id(即
#  graph_config 的 configurable.thread_id)。无 config 的直调场景(测试替身)
#  返回空串 → log_event 静默跳过, 不报错。
def _dialogue_sid(config) -> str:
    try:
        return ((config or {}).get("configurable") or {}).get("thread_id", "") or ""
    except Exception:
        return ""


#  HITL 语义确认 — 自由文本不再按关键词硬匹配, 由 Flash LLM 判断
async def semantic_confirm(request_text: str, answer: str) -> bool:
    """LLM 判断用户对确认请求的回复语义(同意继续/拒绝跳过)。

    Args:
        request_text: interrupt 载荷的确认文案。
        answer: 用户自由文本回复。

    Returns:
        bool: 语义为同意继续时 True;LLM 失败直接抛错(不做兜底)。
    """
    chain = PromptTemplate.from_template(SEMANTIC_CONFIRM_PROMPT) | _structured(
        ConfirmSchema
    )
    v = await chain.ainvoke(
        {"request": request_text[:500], "answer": (answer or "")[:500]}
    )
    return bool(getattr(v, "proceed", False))


# Node 0: Ingest — 每轮请求入口,重置累积字段


def _build_elements(mode: str) -> CaseElements:
    """按模式构建要素集: attorney=婚姻家事要素(现状不动), assistant=文书要素。"""
    from lawApp_LangGraph.prompts import DOC_ELEMENT_DEFS

    if mode == "assistant":
        return CaseElements(
            elements=[
                CaseElement(key=k, label=l, critical=c) for k, l, c in DOC_ELEMENT_DEFS
            ]
        )
    return default_case_elements()


def ingest_node(state: AgentState) -> dict:
    """重置上一轮遗留的计划/结果/累积字段(messages 保留,支撑多轮对话）。"""
    debug.debug("→ 进入 Ingest 节点", detail=f"query={state.query[:60]}")
    mode = state.mode or "attorney"
    return {
        # 模式标识(子项目C: attorney=代理律师咨询 / assistant=律师助理文书起草)
        "mode": mode,
        "doc_type": state.doc_type or "",
        # 覆盖语义字段
        "plan": [],
        "current_step_index": 0,
        "replan_needed": False,
        "replan_reason": None,
        # M3/M8: replanner 空计划标记与用户补充列表一并归位
        "replan_empty": False,
        "user_supplements": [],
        "final_answer": "",
        "final_prompts": "",
        "prompts_record": PromptsRecord(),
        "rag_documents": [],
        "evaluation": EvaluationResult(),
        "pdf_path": None,
        "error": None,
        "risk_confirmed": False,
        "pdf_confirmed": False,
        "hitl_event": None,
        # 子项目A: 要素清单重建 + 澄清/故障计数归零 + 一次性标记复位
        "case_elements": _build_elements(mode),
        "clarify_rounds": 0,
        "error_streak": 0,
        "mid_clarify_used": False,
        "budget_hitl_used": False,
        "degrade_used": False,
        "degrade_ask_count": 0,
        "pending_questions": [],
        # 咨询分类归位(防止上一轮 chitchat 残留路由到闲聊节点)
        "question_category": "marriage_legal",
        # 累积语义字段 → RESET 清空
        "tool_calls": RESET,
        "reasoning": RESET,
        "web_search_results": RESET,
        "law_results": RESET,
        "clarify_history": RESET,
    }


# Node 0.5a: Risk Gate — 高风险话题确认 (HITL)


async def risk_gate_node(state: AgentState, config: RunnableConfig = None) -> dict:
    """LLM 高风险判定;未确认的高风险 → interrupt 确认,拒绝则热线文案中止.

    Args:
        state (AgentState): 图状态,读取 query 与 risk_confirmed.

    Returns:
        dict: 状态更新,各分支语义——空 query 或本轮已确认过
            (risk_confirmed=True) → 空 dict 放行;LLM 判定无风险(含 LLM
            失败软放行) → 空 dict 放行;interrupt 用户拒绝 →
            final_answer(热线中止文案) + risk_confirmed +
            hitl_event(type=risk_confirm, confirmed=False);
            interrupt 用户确认 → risk_confirmed +
            hitl_event(type=risk_confirm, confirmed=True),流程继续.
    """
    t0 = time.time()
    query = state.query.strip()
    if not query or state.risk_confirmed:
        return {}

    high_risk = False
    try:
        chain = PromptTemplate.from_template(RISK_GATE_PROMPT) | _structured(RiskSchema)
        verdict = await chain.ainvoke({"query": query[:2000]})
        high_risk = bool(verdict.high_risk)
    except Exception as e:
        # LLM 失败 → 视为无风险放行(HITL 是增强项不是阻塞项)
        debug.warning("Risk Gate LLM 失败,放行", detail=str(e)[:100])

    if not high_risk:
        debug.debug("← Risk Gate 通过", result=f"elapsed={time.time() - t0:.2f}s")
        return {}

    risk_msg = (
        "您的问题可能涉及人身安全或重大风险。如果您正面临家暴、自伤或紧迫的危险,"
        "请立即拨打110或联系当地妇联/救助机构。确认继续进行AI法律咨询吗?"
    )
    confirmed = interrupt(
        {
            "type": "risk_confirm",
            "message": risk_msg,
            "options": [
                {"value": "确认", "label": "继续咨询"},
                {"value": "跳过", "label": "中止并查看求助热线"},
            ],
        }
    )
    # 方案c: risk_confirm 用户决策落库(resume 消费路径)
    dialogue_log.log_event(
        _dialogue_sid(config),
        "interrupt_confirm",
        {
            "type": "risk_confirm",
            "question": risk_msg,
            "chosen": "继续咨询" if confirmed else "中止并查看求助热线",
        },
    )
    if not confirmed:
        return {
            "final_answer": (
                "已中止本次咨询。请优先保证人身安全:紧急情况拨打110,"
                "家暴可拨打全国妇联维权热线12338,心理困境可拨打希望热线400-161-9995。"
                "安全得到保障后,欢迎随时回来咨询法律问题。"
            ),
            "risk_confirmed": True,
            "hitl_event": {
                "type": "risk_confirm",
                "confirmed": False,
                "at": datetime.now().isoformat(),
            },
        }
    return {
        "risk_confirmed": True,
        "hitl_event": {
            "type": "risk_confirm",
            "confirmed": True,
            "at": datetime.now().isoformat(),
        },
    }


# Node 0.5b: Element Assess — LLM 评估要素缺口 + 解读上轮回答


async def element_assess_node(state: AgentState, config: RunnableConfig = None) -> dict:
    """评估案件要素:1,应用用户上轮回答的要素映射 2,生成下一轮反问.

    Args:
        state (AgentState): 图状态,读取 query / case_elements /
            clarify_history / clarify_rounds.
        config (RunnableConfig): 节点配置,取 thread_id 定位状态总线.

    Returns:
        dict: 状态更新,键语义——case_elements: 深拷贝后应用要素更新
            (回答映射/na 标记/关键级提升)的清单,覆盖写回;
            pending_questions: 本轮反问列表,非空 → 路由继续反问,空 →
            放行进 planner. 分支:非婚姻家事类 → 全要素标 na 直通;
            LLM 失败 → pending_questions 置空,软放行不阻塞.
    """
    t0 = time.time()
    await _publish_node_status(config, "element_assess")
    debug.debug(
        "→ 进入 Element Assess 节点",
        detail=f"round={state.clarify_rounds}/{settings.max_clarify_rounds}",
    )

    ce = state.case_elements.model_copy(deep=True)
    last_q, last_a = "", ""
    if state.clarify_history:
        ex = state.clarify_history[-1]
        last_q, last_a = ex.question, ex.answer

    try:
        chain = PromptTemplate.from_template(ELEMENT_ASSESS_PROMPT) | _structured(
            ElementAssessmentSchema
        )
        v = await chain.ainvoke(
            {
                "query": state.query[:2000],
                "elements_digest": ce.digest(),
                "last_question": last_q,
                "last_answer": last_a or "(尚未反问)",
                "round": state.clarify_rounds + 1,
                "max_rounds": settings.max_clarify_rounds,
            }
        )
    except Exception as e:
        debug.warning("Element Assess LLM 失败,软放行进 planner", detail=str(e)[:100])
        return {"pending_questions": [], "case_elements": ce}

    # 咨询分类(冒烟替身无该字段 → getattr 兜底为 marriage_legal)
    category = getattr(v, "question_category", "marriage_legal") or "marriage_legal"

    # 闲聊/问候 → 不做要素处理,路由直接送闲聊节点出终答
    if category == "chitchat":
        debug.info(
            "← Element Assess: 闲聊类,转 chitchat 节点",
            result=f"elapsed={time.time() - t0:.2f}s",
        )
        return {
            "pending_questions": [],
            "question_category": "chitchat",
            "case_elements": ce,
        }

    # 非婚姻家事类 → 全 na,直接放行
    if not v.applicable:
        ce.mark_na([e.key for e in ce.elements])
        debug.info(
            "← Element Assess: 非目标类咨询,全 na 直通",
            result=f"elapsed={time.time() - t0:.2f}s",
        )
        return {
            "pending_questions": [],
            "question_category": category,
            "case_elements": ce,
        }

    # (2) 应用要素更新(用户回答映射 + na + 关键级提升)
    valid_keys = {e.key for e in ce.elements}
    for u in v.element_updates:
        if u.key in valid_keys:
            ce.update(u.key, u.value, by="assess")
    if v.na_keys:
        ce.mark_na([k for k in v.na_keys if k in valid_keys])
    if v.promote_keys:
        ce.promote([k for k in v.promote_keys if k in valid_keys])

    # (3) 决定是否继续问
    questions = []
    if not v.done and state.clarify_rounds < settings.max_clarify_rounds:
        questions = [q for q in v.questions if q.key in valid_keys][:3]
        # 关键缺口为空时不再问
        if not ce.critical_missing():
            questions = []

    debug.info(
        "← Element Assess 完成",
        detail=f"known={len([e for e in ce.elements if e.status == 'known'])}"
        f"/{len(ce.elements)} | critical_missing={len(ce.critical_missing())}",
        result=f"elapsed={time.time() - t0:.2f}s | {'继续反问' if questions else '放行'}",
    )
    return {
        "case_elements": ce,
        "pending_questions": questions,
        "question_category": category,
    }


# Node 0.5c: Ask Element — HITL-2 要素反问(纯记账,resume 重跑幂等)


def ask_element_node(state: AgentState, config: RunnableConfig = None) -> dict:
    """发起要素反问 interrupt;resume 后记录 clarify_history、轮数自增.

    纯记账节点不调 LLM;无 pending_questions 时直接返回,保证 resume
    重跑幂等. resume 返回值: 非空字符串=用户回答;空/None=跳过(轮数置满).

    每轮只取 pending_questions[0] 单问聚焦(用户决策: 不再把多要素打包
    成一句); q.options 非空时附 options(A/B/C 字母标注)与 allow_other
    供前端渲染推荐选项, 为空则不加键(纯文本反问, 兼容旧载荷消费方).

    Args:
        state (AgentState): 图状态,读取 pending_questions /
            case_elements / clarify_rounds / query.

    Returns:
        dict: 状态更新,各分支语义——无待问问题 → 空 dict 不重复
            interrupt;用户回答 → user_supplements 追加补充(M8: query
            不再改写), clarify_rounds 自增,clarify_history 追加本轮
            ClarifyExchange(含本轮反问的 options),hitl_event(type=
            clarify, question=反问文本);用户跳过 → clarify_rounds 置满
            settings.max_clarify_rounds 按原问题继续,hitl_event(type=
            clarify, skipped=True).
    """
    questions = state.pending_questions
    if not questions:
        return {}  # 防御:无问题不 interrupt

    q = questions[0]  # 单问聚焦: 每轮只问 1 个要素(用户决策)
    question_text = q.question
    keys = [q.key]
    payload = {
        "type": "clarify",
        "round": f"{state.clarify_rounds + 1}/{settings.max_clarify_rounds}",
        "question": question_text,
        "elements": [
            {"key": e.key, "label": e.label, "status": e.status}
            for e in state.case_elements.elements
        ],
    }
    # 推荐选项非空 → 附 A/B/C 字母标注 + allow_other(允许自由输入);
    # 为空不加键, 保持旧 interrupt 载荷形状(兼容存量前端/测试替身)
    q_options = [t for t in (getattr(q, "options", None) or []) if t][:3]
    if q_options:
        payload["options"] = [
            {"value": chr(ord("A") + i), "label": t} for i, t in enumerate(q_options)
        ]
        payload["allow_other"] = True
    # 方案c: round_question 落库(interrupt 前)。resume 会从头重跑节点, 该行
    # 会再次执行 —— dedupe_on 保证同轮同问只写一次(resume 重跑幂等)
    dialogue_log.log_event(
        _dialogue_sid(config),
        "round_question",
        {
            "round": state.clarify_rounds + 1,
            "question": question_text,
            "options": q_options,
        },
        dedupe_on=("round", "question"),
    )
    answer = interrupt(payload)

    answer = str(answer).strip() if answer else ""
    if not answer:
        # 用户跳过 → 轮数置满,按原问题继续(与存量"未补充→按原问题继续"语义一致)
        debug.info("← Ask Element: 用户跳过反问", detail="按原问题继续")
        return {
            "clarify_rounds": settings.max_clarify_rounds,
            "pending_questions": [],
            "hitl_event": {
                "type": "clarify",
                "skipped": True,
                "at": datetime.now().isoformat(),
            },
        }

    # M8: 答案进 user_supplements 列表(query 不再改写 —— 多轮后原问题
    # 会被挤出截断窗, 下游 prompt 统一经 _query_with_supplements 拼接)
    debug.info(
        "← Ask Element 完成",
        detail=f"answer={answer[:80]}",
        result=f"round={state.clarify_rounds + 1}/{settings.max_clarify_rounds}",
    )
    # 方案c: round_answer 落库 —— 答案精确命中某选项 label → option,
    # 否则 free_text(free_text 仅在自由输入时带原文, 选中选项时为 null)
    dialogue_log.log_event(
        _dialogue_sid(config),
        "round_answer",
        {
            "round": state.clarify_rounds + 1,
            "question": question_text,
            "options": q_options,
            "selected": answer,
            "selected_type": "option" if answer in q_options else "free_text",
            "free_text": None if answer in q_options else answer,
            "element_keys": keys,
        },
    )
    return {
        "clarify_rounds": state.clarify_rounds + 1,
        "clarify_history": [
            ClarifyExchange(
                round=state.clarify_rounds + 1,
                question=question_text,
                answer=answer,
                element_keys=keys,
                options=q_options,
            )
        ],
        "user_supplements": [*(getattr(state, "user_supplements", None) or []), answer],
        "hitl_event": {
            "type": "clarify",
            "question": question_text,
            "at": datetime.now().isoformat(),
        },
    }


# Node 0.5d: Chitchat — 闲聊轻量应答(question_category=chitchat 专用)


async def chitchat_node(state: AgentState, config: RunnableConfig = None) -> dict:
    """闲聊应答节点 — Flash LLM 流式生成 1~2 句友好回应并引导法律提问.

    由 route_after_assess 在 question_category=chitchat 时路由进入,
    不经 planner/检索,直接写 final_answer 后结束(省规划与检索成本).
    LLM 失败直接抛错(用户约束: 不做兜底).

    Args:
        state (AgentState): 图状态,读取 query.
        config (RunnableConfig): 节点配置,取 thread_id 定位状态总线.

    Returns:
        dict: 状态更新,final_answer 为闲聊回应文本.
    """
    t0 = time.time()
    await _publish_node_status(config, "chitchat")
    debug.debug("→ 进入 Chitchat 节点", detail=f"query={state.query[:60]}")

    chain = PromptTemplate.from_template(CHITCHAT_PROMPT) | get_executor_llm()
    parts: list[str] = []
    async for chunk in chain.astream({"query": state.query[:1000]}):
        parts.append(chunk.content or "")
    answer = "".join(parts).strip()

    debug.info(
        "← Chitchat 完成",
        detail=f"answer_len={len(answer)}",
        result=f"elapsed={time.time() - t0:.2f}s",
    )
    return {"final_answer": answer}


# Node 1: The Planner — Pro LLM 制定计划 + 思考链


# 拼工具清单摘要(名字+描述前 120 字)喂 planner 提示词
def _tools_desc() -> str:
    return "\n".join(f"- {t.name}: {(t.description or '')[:120]}" for t in ALL_TOOLS())


# 结构化计划 → PlanStep 列表, 未知工具名置 None 交 executor 自行处理
def _normalize_plan(schema) -> list[PlanStep]:
    """将 structured output 的计划规范为 PlanStep 列表(过滤未知工具名）。"""
    steps: list[PlanStep] = []
    for p in schema.plan:
        tn = p.tool_name
        if tn and _tool_by_name(tn) is None:
            tn = None
        steps.append(
            PlanStep(
                step_id=p.step_id or len(steps) + 1,
                description=p.description,
                tool_name=tn,
            )
        )
    return steps


# M8: 用户补充信息视图 — HITL 答案不再拼进 state.query(多轮后原问题被挤出
# 截断窗), 改存 user_supplements 列表; prompt 组装时在 query 截断之后拼接,
# 每条截 500 字、总量上限 2000 字
_SUPPLEMENT_PER_ITEM_LIMIT = 500
_SUPPLEMENT_TOTAL_LIMIT = 2000


def _query_with_supplements(state: AgentState, limit: int = 0) -> str:
    """原 query(可选截断) + 用户补充信息的拼接视图(planner/replanner/
    executor/replan_check 的 prompt 统一走此视图)。"""
    base = (state.query or "")[:limit] if limit else (state.query or "")
    parts = [base] if base else []
    total = 0
    for s in getattr(state, "user_supplements", None) or []:
        piece = (s or "")[:_SUPPLEMENT_PER_ITEM_LIMIT]
        if not piece:
            continue
        if total + len(piece) > _SUPPLEMENT_TOTAL_LIMIT:
            piece = piece[: _SUPPLEMENT_TOTAL_LIMIT - total]
            if piece:
                parts.append(f"[用户补充信息] {piece}")
            break
        parts.append(f"[用户补充信息] {piece}")
        total += len(piece)
    return "\n".join(parts)


# regex 截 JSON 主体 + PlanSchema 校验; 失败直接 raise(用户约束: 不做兜底)
def _parse_plan_json(raw: str) -> "PlanSchema":
    """解析 reasoner 流式累积的 content 为 PlanSchema;失败直接抛错(不做兜底)。"""
    import json
    import re

    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        raise ValueError(f"Planner 输出中未找到 JSON 对象: {raw[:200]!r}")
    return PlanSchema.model_validate(json.loads(m.group(0)))


# M2: planner/replanner 共用的裸 openai 客户端 — 模块级懒加载单例。
# 旧实现每次 _stream_plan 都 openai.AsyncOpenAI(...) 新建且从不 close
# (泄漏); 无 timeout 时 DeepSeek 挂住 → planner 节点永挂。单例 + 60s 超时。
_PLANNER_OPENAI_TIMEOUT = 60.0
_planner_openai_client = None
_planner_openai_lock = threading.Lock()


def _get_planner_openai_client():
    """DeepSeek reasoner 裸流式客户端(懒加载单例, timeout=60s)。"""
    global _planner_openai_client
    if _planner_openai_client is None:
        with _planner_openai_lock:
            if _planner_openai_client is None:
                import openai

                _planner_openai_client = openai.AsyncOpenAI(
                    api_key=settings.deepseek_api_key,
                    base_url=settings.deepseek_base_url or None,
                    timeout=_PLANNER_OPENAI_TIMEOUT,
                )
    return _planner_openai_client


# raw openai SDK 流式跑 planner/replanner: reasoning 逐帧推 CoT 总线, content 拼齐后解析计划
async def _stream_plan(
    prompt_text: str, source: str, config: RunnableConfig
) -> tuple["PlanSchema", list[str]]:
    """手工流式调用 reasoner: reasoning_content 逐字推 CoT 总线,累积 content 手动解析。

    直接走 openai SDK 裸流: langchain_openai 的流式解析会丢弃 DeepSeek reasoner 的
    非标准 delta.reasoning_content 字段, 必须自取。

    Args:
        prompt_text: 已 format 好的完整提示词。
        source: 事件来源标记, "planner" 或 "replanner"。
        config: 节点 config, 取 configurable.thread_id 定位总线。

    Returns:
        (解析成功的 PlanSchema, reasoning 增量列表)。

    Raises:
        ValueError: content 无法解析为 JSON 或不符合 PlanSchema 时直接抛出。
    """
    import openai

    llm = get_planner_llm()
    try:
        from langchain_openai import ChatOpenAI

        real_llm = isinstance(llm, ChatOpenAI)
    except ImportError:
        real_llm = False
    if not real_llm:
        # 冒烟测试替身: 无 openai 裸客户端可流, 走替身 with_structured_output
        # 原签名, 不推 CoT 总线(reasoning 由替身 verdict 携带)
        verdict = await llm.with_structured_output(PlanSchema).ainvoke(prompt_text)
        return verdict, list(verdict.reasoning or [])

    thread_id = (config.get("configurable") or {}).get("thread_id", "")
    reasoning: list[str] = []
    content: list[str] = []
    # M2: 模块级单例(带 60s 超时), planner/replanner/解析失败重试共用一个客户端
    client = _get_planner_openai_client()
    stream = await client.chat.completions.create(
        model=settings.deepseek_pro_model,
        messages=[{"role": "user", "content": prompt_text}],
        stream=True,
    )
    async for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        rc = getattr(delta, "reasoning_content", None) or ""
        if rc:
            reasoning.append(rc)
            # CoT 增量经总线广播(H4: 同会话全部订阅队列 fan-out)
            _bus_put(thread_id, {"source": source, "delta": rc})
        if delta.content:
            content.append(delta.content)
            # 计划内容实时流(先思考后计划): source 加 _plan 后缀区分, 前端路由到计划面板
            _bus_put(thread_id, {"source": f"{source}_plan", "delta": delta.content})
    return _parse_plan_json("".join(content)), reasoning


async def planner_node(state: AgentState, config: RunnableConfig) -> dict:
    """Pro reasoner 流式: 生成计划 + reasoning_content 推 CoT 总线;解析失败直接报错。"""
    t0 = time.time()
    query = state.query.strip()
    await _publish_node_status(config, "planner")
    debug.debug("→ 进入 Planner 节点", detail=f"query={query[:80]}")

    if not query:
        debug.info("← Planner 退出", detail="空输入", result="返回默认提示")
        return {
            "plan": [],
            "reasoning": ["无输入"],
            "final_answer": "抱一丝,你能再说一遍吗?",
        }

    template = PLANNER_SYSTEM

    if (state.mode or "attorney") == "assistant":
        from lawApp_LangGraph.prompts import PLANNER_ASSISTANT_SUFFIX

        template = PLANNER_SYSTEM + PLANNER_ASSISTANT_SUFFIX.format(
            doc_type_label="起诉状" if state.doc_type != "defense" else "答辩状"
        )

    prompt = PromptTemplate.from_template(template).format(
        # M8: 原问题截断后拼用户补充信息(HITL 答案不再改写 query)
        query=_query_with_supplements(state, 3000),
        available_tools=_tools_desc(),
        elements_digest=state.case_elements.digest(),
    )

    result, _cot = await _stream_plan(prompt, "planner", config)

    plan = _normalize_plan(result)

    reasoning = list(result.reasoning or [])

    elapsed = time.time() - t0
    step_names = [f"{s.step_id}.{s.tool_name or '闲聊'}" for s in plan]
    debug.info(
        "← Planner 完成",
        detail=f"reasoning={len(reasoning)}条, plan={len(plan)}步",
        result=f"elapsed={elapsed:.2f}s | 步骤: {' → '.join(step_names) if step_names else '无(直接回答)'}",
    )
    return {
        "plan": plan,
        "reasoning": reasoning,
    }


# Node 2: The Executor — Flash LLM 为当前步骤生成工具调用


# 汇总 plan 步骤执行简况(供 replanner/finalize 提示词)
def _step_summaries(state: AgentState) -> dict[str, str]:
    rag_summary = "暂无"
    if state.rag_documents:
        rag_summary = " | ".join(
            f"[{d.hybrid_score:.2f}] {d.chunk_text[:100]}..."
            for d in state.rag_documents[:3]
        )
    eval_summary = "未评估"
    if state.evaluation and state.evaluation.total > 0:
        ev = state.evaluation
        eval_summary = (
            f"共{ev.total}条,高质量{ev.correct_count},中等{ev.ambiguous_count},"
            f"低质量{ev.incorrect_count},结论: {ev.quality_verdict}"
        )
    web_summary = "暂无"
    if state.web_search_results:
        parts = []
        for item in state.web_search_results[-3:]:
            title = (
                item.get("title", "")
                if isinstance(item, dict)
                else getattr(item, "title", "")
            )
            snippet = (
                item.get("snippet", "")
                if isinstance(item, dict)
                else getattr(item, "snippet", "")
            )
            parts.append(f"[{title}] {snippet[:80]}...")
        web_summary = " | ".join(parts)
    law_summary = "暂无"
    if state.law_results:
        law_summary = " | ".join(
            f"[{law.law_title}] 第{law.article_number}条 {law.content[:80]}..."
            for law in state.law_results[:5]
        )
    return {
        "rag_summary": rag_summary,
        "eval_summary": eval_summary,
        "web_summary": web_summary,
        "law_summary": law_summary,
    }


async def executor_node(state: AgentState, config: RunnableConfig = None) -> dict:
    """Flash LLM 为当前步骤生成工具调用(AIMessage + tool_calls）。

    - 无工具步骤 → 直接标记完成并推进
    - markdown_to_pdf 且未确认 → interrupt 请求用户确认(HITL-3,
      前端单选+确认/跳过按钮; 自由文本由 normalize_resume 的 LLM 语义判断)
    - LLM 未发起调用 → 严格重试一次；再失败则标记步骤 failed 并推进

    Args:
        state (AgentState): 图状态。
        config (RunnableConfig): 节点配置,取 thread_id 定位状态总线。
    """
    t0 = time.time()
    idx = state.current_step_index
    plan = state.plan

    if idx >= len(plan):
        return {}

    step = plan[idx]
    total_steps = len(plan)
    await _publish_status(
        config,
        f"正在执行步骤 {idx + 1}/{total_steps}: {step.description[:40]}"
        + (f"(工具 {step.tool_name})" if step.tool_name else ""),
    )
    debug.debug(
        f"→ 进入 Executor 节点 [{idx + 1}/{total_steps}]",
        detail=f"tool_name={step.tool_name or '无'} | desc={step.description[:60]}",
    )

    # 无工具步骤 → 跳过并推进
    if not step.tool_name:
        done = [
            s.model_copy(update={"status": "done"}) if i == idx else s
            for i, s in enumerate(plan)
        ]
        return {"plan": done, "current_step_index": idx + 1}

    # HITL-3: PDF 生成前确认(前端单选+确认/跳过; 自由文本由 LLM 语义判断归一为 bool)
    if step.tool_name == "markdown_to_pdf" and not state.pdf_confirmed:
        pdf_msg = f"即将生成 PDF 报告(步骤: {step.description})。确认生成吗?"
        confirmed = interrupt(
            {
                "type": "pdf_confirm",
                "message": pdf_msg,
                "options": [
                    {"value": "确认", "label": "确认生成 PDF"},
                    {"value": "跳过", "label": "跳过该步骤"},
                ],
            }
        )
        # 方案c: pdf_confirm 用户决策落库(resume 消费路径)
        dialogue_log.log_event(
            _dialogue_sid(config),
            "interrupt_confirm",
            {
                "type": "pdf_confirm",
                "question": pdf_msg,
                "chosen": "确认生成 PDF" if confirmed else "跳过该步骤",
            },
        )
        if not confirmed:
            done = [
                s.model_copy(update={"status": "done"}) if i == idx else s
                for i, s in enumerate(plan)
            ]
            debug.info("← Executor PDF 步骤被用户跳过", detail=f"step={idx + 1}")
            return {
                "plan": done,
                "current_step_index": idx + 1,
                "pdf_confirmed": True,
                "hitl_event": {
                    "type": "pdf_confirm",
                    "confirmed": False,
                    "at": datetime.now().isoformat(),
                },
            }
        # 确认 → 继续 LLM 参数提取

    tool = _tool_by_name(step.tool_name)
    if tool is None:
        # 旧 checkpoint 引用已消失的工具(如 MCP server 下线)→ 步骤记 failed,
        # 不裸抛 KeyError 打穿整次执行(H1;status 对齐既有失败分支的 Literal)
        errored = [
            s.model_copy(update={"status": "failed", "retry_count": s.retry_count + 1})
            if i == idx
            else s
            for i, s in enumerate(plan)
        ]
        debug.warning(
            "← Executor 工具不可用,标记步骤 failed",
            detail=f"tool={step.tool_name} | step={idx + 1}",
        )
        return {
            "plan": errored,
            "current_step_index": idx + 1,
            "error": f"步骤{step.step_id}({step.tool_name}) 工具 {step.tool_name} 不可用",
            "error_streak": state.error_streak + 1,
            "messages": [AIMessage(content="")],
        }
    summaries = _step_summaries(state)
    prompt = EXECUTOR_PROMPT.format(
        step_description=step.description,
        tool_name=step.tool_name,
        # M8: 决策用 query 统一走带用户补充的视图
        user_query=_query_with_supplements(state),
        elements_digest=state.case_elements.digest(),
        **summaries,
    )

    ai_msg: Optional[AIMessage] = None
    try:
        llm = get_executor_llm()
        response = await llm.bind_tools([tool]).ainvoke([SystemMessage(content=prompt)])
        if isinstance(response, AIMessage) and response.tool_calls:
            ai_msg = response
        else:
            raise RuntimeError("Flash LLM 未发起工具调用")
    except Exception as first_err:
        # 严格重试一次(取代旧版 _TOOL_FALLBACK_ARGS 参数映射）
        try:
            retry_prompt = f"{prompt}\n\n注意:上一次调用失败({str(first_err)[:80]}).必须立即调用工具 {step.tool_name}."
            response = (
                await get_executor_llm()
                .bind_tools([tool])
                .ainvoke([SystemMessage(content=retry_prompt)])
            )
            if isinstance(response, AIMessage) and response.tool_calls:
                ai_msg = response
        except Exception as second_err:
            debug.warning(
                "Executor LLM 两次调用失败,标记步骤 failed",
                detail=f"tool={step.tool_name} | err={str(second_err)[:100]}",
            )

    # 失败路径:标记步骤 failed、推进索引、追加空 AIMessage 防止 ToolNode 误路由
    if ai_msg is None:
        failed = [
            s.model_copy(update={"status": "failed", "retry_count": s.retry_count + 1})
            if i == idx
            else s
            for i, s in enumerate(plan)
        ]
        return {
            "plan": failed,
            "current_step_index": idx + 1,
            "error": f"步骤{step.step_id}({step.tool_name}) LLM 参数提取失败",
            "error_streak": state.error_streak + 1,
            "messages": [AIMessage(content="")],
        }

    # analyze_legal_issue 需要完整 PromptsRecord,LLM 无法自行构造 → 注入
    if step.tool_name == "analyze_legal_issue" and state.prompts_record:
        tc = dict(ai_msg.tool_calls[0])
        args = {
            **tc.get("args", {}),
            "prompts_record": state.prompts_record.model_dump(),
        }
        tc["args"] = args
        ai_msg = AIMessage(content=ai_msg.content, tool_calls=[tc])

    doing = [
        s.model_copy(update={"status": "doing"}) if i == idx else s
        for i, s in enumerate(plan)
    ]
    debug.info(
        "← Executor 生成工具调用",
        detail=f"tool={step.tool_name} | args={str(ai_msg.tool_calls[0]['args'])[:150]}",
        result=f"elapsed={time.time() - t0:.2f}s | → tools",
    )
    # 注意: current_step_index 由 merge 节点在工具执行完成后推进
    return {"plan": doing, "messages": [ai_msg]}


# Node 3: Tools — prebuilt ToolNode 执行工具(构建时读取此刻 ALL_TOOLS() 快照,含 MCP 工具)
# 外包一层异步节点: 工具执行前向 SSE 通道推工作状态(用户决策 v4)


def _build_tools_node():
    inner = ToolNode(ALL_TOOLS(), handle_tool_errors=True)

    async def tools_node(state: AgentState, config: RunnableConfig = None) -> dict:
        idx = state.current_step_index
        step = state.plan[idx] if idx < len(state.plan) else None
        if step is not None and step.tool_name:
            await _publish_status(config, f"工具 {step.tool_name} 执行中...")
        return await inner.ainvoke(state, config)

    return tools_node


# Node 4: Merge — 合并工具结果到 AgentState,推进步骤索引


# 从 dict/对象取字段, 缺省回退空串(merge 解析工具结果用)
def _field(item, key: str, default: str = ""):
    """从 dict 或 Pydantic 对象中安全提取字段值."""
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


async def merge_node(state: AgentState, config: RunnableConfig = None) -> dict:
    """解析 ToolMessage(dict 经 json.dumps 序列化)→ 合并到 state 字段,
    记录 ToolCallRecord,推进 current_step_index。"""
    t0 = time.time()
    await _publish_node_status(config, "merge")
    idx = state.current_step_index
    plan = state.plan
    step = plan[idx]

    updates: Dict[str, Any] = {}
    step_status = "done"
    output: Any = None

    # 找最近一条 ToolMessage(ToolNode 追加在 messages 尾部)
    tool_msg: Optional[ToolMessage] = None
    for m in reversed(state.messages):
        if isinstance(m, ToolMessage):
            tool_msg = m
            break
        if isinstance(m, AIMessage):
            break

    if tool_msg is None:
        step_status = "failed"
        updates["error"] = f"步骤{step.step_id} 工具未返回结果"
        updates["error_streak"] = state.error_streak + 1
    elif getattr(tool_msg, "status", None) == "error":
        step_status = "failed"
        updates["error"] = (
            f"工具 {step.tool_name} 执行失败: {str(tool_msg.content)[:200]}"
        )
        updates["error_streak"] = state.error_streak + 1
        output = {"error": str(tool_msg.content)[:500]}
    else:
        try:
            content = tool_msg.content
            output = json.loads(content) if isinstance(content, str) else content
        except (json.JSONDecodeError, TypeError):
            output = {"raw": str(tool_msg.content)[:500]}
        # 成功路径: 连续失败计数清零
        updates["error_streak"] = 0

    if isinstance(output, dict):
        for k, v in output.items():
            if k in _STATE_KEYS:
                # web/law 为 append reducer → 只传增量
                updates[k] = v

    # tool_calls / ToolCallRecord 增量追加
    if step.tool_name:
        executed_args: Dict[str, Any] = {}
        for m in reversed(state.messages):
            if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
                executed_args = dict(m.tool_calls[0].get("args", {}))
                break
        updates["tool_calls"] = [
            ToolCallRecord(
                step_id=step.step_id,
                tool_name=step.tool_name or "",
                tool_input=executed_args,
                output=(
                    output
                    if not isinstance(output, dict)
                    else {
                        k: v for k, v in output.items() if k not in ("prompts_record",)
                    }
                ),
            )
        ]

    # 重建 PromptsRecord(累积态完整写入,供 analyze_legal_issue 使用)
    merged_web = output.get("web_search_results") if isinstance(output, dict) else None
    merged_web = merged_web or []
    merged_law = output.get("law_results") if isinstance(output, dict) else None
    merged_law = merged_law or []
    merged_eval = (
        output.get("evaluation", state.evaluation)
        if isinstance(output, dict)
        else state.evaluation
    )
    eval_docs: list = []
    if merged_eval:
        if isinstance(merged_eval, dict):
            eval_docs = list(merged_eval.get("correct", [])) + list(
                merged_eval.get("ambiguous", [])
            )
        else:
            eval_docs = list(merged_eval.correct) + list(merged_eval.ambiguous)

    updates["prompts_record"] = PromptsRecord(
        query=state.query,
        web_search_results=merged_web,
        laws_results=merged_law,
        evaluate_retrieved_documents=eval_docs,
        known_elements=state.case_elements.digest(),
    )

    new_plan = [
        s.model_copy(update={"status": step_status}) if i == idx else s
        for i, s in enumerate(plan)
    ]
    updates["plan"] = new_plan
    updates["current_step_index"] = idx + 1

    debug.info(
        f"← Merge 完成 [{idx + 1}/{len(plan)}]",
        detail=f"tool={step.tool_name} | status={step_status}",
        result=f"elapsed={time.time() - t0:.2f}s",
    )
    return updates


# Node 5: Replan Check — Flash LLM 质量门控


async def replan_check_node(state: AgentState, config: RunnableConfig = None) -> dict:
    """Flash LLM 语义判断是否需要重规划；失败降级为规则判断。"""
    t0 = time.time()
    await _publish_node_status(config, "replan_check")
    debug.debug("→ 进入 Replan Check 节点", detail="LLM 语义判断执行质量...")

    # M3 兜底: 空 plan 不允许再触发 replanner(executor 空转 → 死循环),
    # 按「材料不足」直接放行 finalize
    if not state.plan:
        debug.warning("← Replan Check: 计划为空,按材料不足收尾")
        return {
            "replan_needed": False,
            "replan_reason": "计划为空,材料不足",
            "insufficient_reason": "none",
        }

    steps_desc = []
    for s in state.plan:
        label = "✓" if s.status == "done" else "✗" if s.status == "failed" else "⋯"
        steps_desc.append(
            f"[{label}] 步骤{s.step_id}: {s.description} → 工具: {s.tool_name or '无'}"
        )
    quality_verdict = "未评估"
    if state.evaluation and state.evaluation.total > 0:
        ev = state.evaluation
        quality_verdict = (
            f"{ev.quality_verdict} (共{ev.total}条, 高质量{ev.correct_count}, "
            f"中等{ev.ambiguous_count}, 低质量{ev.incorrect_count})"
        )

    needs, reason, insufficient_reason = False, "", "none"
    try:
        chain = PromptTemplate.from_template(REPLAN_CHECK_PROMPT) | _structured(
            ReplanCheckSchema
        )
        result = await chain.ainvoke(
            {
                # M8: 决策用 query 统一走带用户补充的视图
                "user_query": _query_with_supplements(state, 1000),
                "executed_summary": "\n".join(steps_desc) or "无已执行步骤",
                "doc_count": len(state.rag_documents),
                "quality_verdict": quality_verdict,
                "web_count": len(state.web_search_results),
                "law_count": len(state.law_results),
                "error_info": state.error or "无",
            }
        )
        needs = bool(result.needs_replan)
        reason = result.reason
        insufficient_reason = (
            result.insufficient_reason
            if needs and hasattr(result, "insufficient_reason")
            else "none"
        )
    except Exception as e:
        debug.warning("Replan Check LLM 失败,降级为规则判断", detail=str(e)[:100])
        needs, reason, insufficient_reason = _fallback_replan_check(state)

    elapsed = time.time() - t0
    target = "Replanner" if needs else "Finalize"
    debug.info(
        f"← Replan Check: {'需要重规划' if needs else '质量通过'}",
        detail=reason,
        result=f"elapsed={elapsed:.2f}s | → {target}",
    )
    return {
        "replan_needed": needs,
        "replan_reason": reason or None,
        "insufficient_reason": insufficient_reason,
    }


# replan_check 的规则兜底: LLM 判断失败时按 评估不足且未联网 → not_found/vague 判定
def _fallback_replan_check(state: AgentState) -> tuple[bool, str, str]:
    """规则兜底判断 —— LLM 判断失败时使用.

    Args:
        state (AgentState): 图状态,读取 error / evaluation / tool_calls /
            rag_documents.

    Returns:
        tuple[bool, str, str]: (needs_replan, replan_reason, insufficient_reason)
            三元组,insufficient_reason 取值 vague/not_found/error/none:
            执行错误 → (True, 异常摘要, "error");评估结论不足且尚未联网 →
            检索为空记 "not_found"(案例库覆盖不到),有检索但问题笼统记
            "vague";其余 → (False, 无明显问题, "none").
    """
    if state.error:
        return True, f"执行异常: {state.error[:60]}", "error"
    if (
        state.evaluation
        and state.evaluation.quality_verdict == "不足,建议进行网络搜索补充"
    ):
        executed = {tc.tool_name for tc in state.tool_calls}
        if "get_google_search" not in executed:
            if not state.rag_documents:
                return True, "检索为空,案例库覆盖不到", "not_found"
            return True, "检索质量不足且问题笼统", "vague"
    return False, "规则兜底: 无明显问题", "none"


# Node 5.5: Mid Clarify — HITL-5 检索反馈追问(先问人后搜网)


async def mid_clarify_node(state: AgentState, config: RunnableConfig = None) -> dict:
    """基于检索结果的共同情形生成聚焦追问,先问人后搜网.

    Args:
        state (AgentState): 图状态,读取 query / rag_documents / case_elements.
        config (RunnableConfig): 节点配置,取 thread_id 定位状态总线.

    Returns:
        dict: 状态更新,分支语义——LLM 生成追问失败或用户未补充 →
            {"mid_clarify_used": True} 静默放行,由 replanner 联网兜底;
            用户补充 → user_supplements 追加「[检索反馈追问]/[用户澄清]」
            (M8: query 不改写), case_elements 深拷贝后按 element_key 记录
            (来源 mid_clarify), 附 hitl_event(type=mid_clarify, question=追问文本).
    """
    t0 = time.time()
    await _publish_node_status(config, "mid_clarify")
    top_docs = (
        "\n".join(
            f"- [{d.case_number}] {d.chunk_text[:120]}..."
            for d in state.rag_documents[:5]
        )
        or "(检索为空)"
    )

    try:
        chain = PromptTemplate.from_template(MID_CLARIFY_PROMPT) | _structured(
            MidClarifySchema
        )
        v = await chain.ainvoke(
            {
                "query": state.query[:1500],
                "top_docs_summary": top_docs,
            }
        )
    except Exception as e:
        debug.warning("Mid Clarify LLM 失败,转联网兜底", detail=str(e)[:100])
        return {"mid_clarify_used": True}

    # 推荐选项非空 → 附 A/B/C 字母标注 + allow_other(允许自由输入);
    # 为空不加键, 保持旧 interrupt 载荷形状(getattr 兜底测试替身缺字段)
    v_options = [t for t in (getattr(v, "options", None) or []) if t][:3]
    payload = {
        "type": "mid_clarify",
        "question": v.question,
        "context_hint": f"检索到的案例集中在: {top_docs[:200]}",
    }
    if v_options:
        payload["options"] = [
            {"value": chr(ord("A") + i), "label": t} for i, t in enumerate(v_options)
        ]
        payload["allow_other"] = True
    # 方案c: round_question 落库(interrupt 前; dedupe_on 防 resume 重跑重复写)
    dialogue_log.log_event(
        _dialogue_sid(config),
        "round_question",
        {
            "round": state.clarify_rounds + 1,
            "question": v.question,
            "options": v_options,
        },
        dedupe_on=("round", "question"),
    )
    answer = interrupt(payload)
    answer = str(answer).strip() if answer else ""
    if not answer:
        debug.info("← Mid Clarify: 用户未补充", detail="转联网兜底")
        return {"mid_clarify_used": True}

    # M8: 追问与澄清进 user_supplements(原 query 不改写), 下游 prompt
    # 经 _query_with_supplements 拼接
    ce = state.case_elements.model_copy(deep=True)
    if v.element_key in {e.key for e in ce.elements}:
        ce.update(v.element_key, answer, by="mid_clarify")
    debug.info(
        "← Mid Clarify 完成",
        detail=f"answer={answer[:80]}",
        result=f"elapsed={time.time() - t0:.2f}s | → replanner",
    )
    # 方案c: round_answer 落库(选项精确匹配 → option, 否则 free_text)
    dialogue_log.log_event(
        _dialogue_sid(config),
        "round_answer",
        {
            "round": state.clarify_rounds + 1,
            "question": v.question,
            "options": v_options,
            "selected": answer,
            "selected_type": "option" if answer in v_options else "free_text",
            "free_text": None if answer in v_options else answer,
            "element_keys": [v.element_key],
        },
    )
    return {
        "mid_clarify_used": True,
        "user_supplements": [
            *(getattr(state, "user_supplements", None) or []),
            f"[检索反馈追问] {v.question}\n[用户澄清] {answer}",
        ],
        "case_elements": ce,
        "hitl_event": {
            "type": "mid_clarify",
            "question": v.question,
            "at": datetime.now().isoformat(),
        },
    }


# 已执行工具及结果摘要(replanner 提示词上下文, 用户决策 v4)
def _tool_calls_digest(state: AgentState) -> str:
    lines = []
    for tc in state.tool_calls:
        out = tc.output
        if isinstance(out, dict):
            parts = []
            for k, v in out.items():
                if isinstance(v, list):
                    parts.append(f"{k}×{len(v)}")
                else:
                    parts.append(f"{k}: {str(v)[:60]}")
            out_str = ", ".join(parts)
        else:
            out_str = str(out)[:80]
        lines.append(f"- {tc.tool_name} → {out_str or '无输出'}")
    return "\n".join(lines) or "尚未执行任何工具"


# Node 6: The Replanner — Pro LLM 补充计划


async def replanner_node(state: AgentState, config: RunnableConfig) -> dict:
    """Pro reasoner 流式: 生成补充计划 + reasoning_content 推 CoT 总线;解析失败直接报错。"""
    t0 = time.time()
    reason = state.replan_reason or "质量不足"
    await _publish_node_status(config, "replanner")
    debug.debug(
        "→ 进入 Replanner 节点", detail=f"原因: {reason} | 已完成{len(state.plan)}步"
    )

    executed = "\n".join(
        f"[{'done' if s.status == 'done' else s.status}] "
        f"步骤{s.step_id}: {s.description} → {s.tool_name}"
        for s in state.plan
    )

    prompt = REPLANNER_SYSTEM_PROMPT.format(
        executed_steps=executed or "无",
        tool_calls_digest=_tool_calls_digest(state),
        doc_count=len(state.rag_documents),
        quality=state.evaluation.quality_verdict if state.evaluation else "未评估",
        web_count=len(state.web_search_results),
        law_count=len(state.law_results),
        error=state.error or "无",
        replan_reason=reason,
        available_tools=_tools_desc(),
        user_query=_query_with_supplements(state, 2000),
        next_id=len(state.plan) + 1,
    )

    result, _cot = await _stream_plan(prompt, "replanner", config)
    additional = [
        PlanStep(
            step_id=len(state.plan) + i + 1,
            description=p.description,
            tool_name=(p.tool_name if _tool_by_name(p.tool_name) is not None else None),
        )
        for i, p in enumerate(result.plan)
    ][:3]
    new_reasoning = [f"[Replan] {reason}"] + list(result.reasoning or [])

    # M3: LLM 返回空补充计划 → 按「材料不足」收尾, 不再回 executor 空转。
    # 旧路径: executor idx>=len(plan) 返回 {} → replan_check 又要 replan →
    # 循环直至 recursion_limit 整轮 GraphRecursionError
    if not additional:
        debug.warning(
            "← Replanner 空 plan,按材料不足直接收尾",
            detail=f"已有步骤{len(state.plan)}步无新增",
            result=f"elapsed={time.time() - t0:.2f}s | → finalize",
        )
        return {
            "plan": list(state.plan),
            "reasoning": new_reasoning,
            "replan_needed": False,
            "replan_reason": "重规划未产生新步骤,材料不足,基于现有材料收尾",
            "error": None,
            "replan_empty": True,
        }

    elapsed = time.time() - t0
    new_names = [f"{s.step_id}.{s.tool_name}" for s in additional]
    debug.info(
        "← Replanner 完成",
        detail=f"新增{len(additional)}步: {' , '.join(new_names)}",
        result=f"elapsed={elapsed:.2f}s | → Executor",
    )
    return {
        "plan": list(state.plan) + additional,
        "reasoning": new_reasoning,
        "replan_needed": False,
        "replan_reason": None,
        "error": None,
        "replan_empty": False,
    }


# Node 7: Finalize — 组装最终回答

# 方案c: final_answer 事件的引用来源列表(law_results 法条 + rag_documents
# 案例的标题/编号名称, 空则 [])
def _final_citations(state: AgentState) -> list:
    cites = [f"{l.law_title} {l.article_number}".strip() for l in (state.law_results or [])]
    cites += [(d.case_number or d.case_cause or "") for d in (state.rag_documents or [])]
    return [c for c in cites if c]


async def finalize_node(state: AgentState, config: RunnableConfig = None) -> dict:
    """优先复用已有 final_answer；否则用案例兜底生成 / LLM 直接回答(流式）。"""
    t0 = time.time()
    await _publish_node_status(config, "finalize")
    debug.debug("→ 进入 Finalize 节点", detail="组装最终回答...")

    if state.final_answer:
        # 方案c: final_answer 落库(答案已由 risk_gate/降级中止等前置节点写好)
        dialogue_log.log_event(
            _dialogue_sid(config),
            "final_answer",
            {
                "answer": state.final_answer,
                "citations": _final_citations(state),
                "clarify_rounds": state.clarify_rounds,
            },
        )
        debug.info(
            "← Finalize 完成 (已有答案)",
            detail=f"answer_len={len(state.final_answer)}",
            result=f"elapsed={time.time() - t0:.2f}s",
        )
        return {}

    if (state.mode or "attorney") == "assistant":
        from lawApp_LangGraph.prompts import (
            FINALIZE_COMPLAINT_PROMPT,
            FINALIZE_DEFENSE_PROMPT,
        )

        template = (
            FINALIZE_DEFENSE_PROMPT
            if state.doc_type == "defense"
            else FINALIZE_COMPLAINT_PROMPT
        )
        laws_digest = (
            "\n".join(
                f"{l.law_title} {l.article_number}: {l.content[:80]}"
                for l in (state.law_results or [])[:5]
            )
            or "无"
        )
        cases_digest = (
            "\n".join(d.chunk_text[:100] for d in (state.rag_documents or [])[:3])
            or "无"
        )
        chain = PromptTemplate.from_template(template) | get_executor_llm()
        parts = []
        async for chunk in chain.astream(
            {
                "elements_digest": state.case_elements.digest(),
                # M8: 带用户补充的视图(与旧"补充拼进 query"的行为对齐)
                "query": _query_with_supplements(state, 3000),
                "laws_digest": laws_digest,
                "cases_digest": cases_digest,
            }
        ):
            parts.append(chunk.content or "")
        answer = "".join(parts)
        debug.info(
            "← Finalize 完成 (文书起草)",
            detail=f"doc_type={state.doc_type or 'complaint'}, answer_len={len(answer)}",
            result=f"elapsed={time.time() - t0:.2f}s",
        )
        # 方案c: final_answer 落库(文书起草)
        dialogue_log.log_event(
            _dialogue_sid(config),
            "final_answer",
            {
                "answer": answer,
                "citations": _final_citations(state),
                "clarify_rounds": state.clarify_rounds,
            },
        )
        return {"final_answer": answer}

    if state.rag_documents:
        docs = "\n".join(f"- {d.chunk_text[:300]}" for d in state.rag_documents[:3])
        chain = FINALIZE_CASE_PROMPT | get_executor_llm()
        parts: list[str] = []
        async for chunk in chain.astream(
            {"docs": docs, "query": _query_with_supplements(state)}
        ):
            parts.append(chunk.content or "")
        answer = "".join(parts)
        debug.info(
            "← Finalize 完成 (案例兜底)",
            detail=f"answer_len={len(answer)}",
            result=f"elapsed={time.time() - t0:.2f}s",
        )
        # 方案c: final_answer 落库(案例兜底)
        dialogue_log.log_event(
            _dialogue_sid(config),
            "final_answer",
            {
                "answer": answer,
                "citations": _final_citations(state),
                "clarify_rounds": state.clarify_rounds,
            },
        )
        return {"final_answer": answer}

    chain = FINALIZE_DIRECT_PROMPT | get_executor_llm()
    parts = []
    async for chunk in chain.astream(
        {"query": _query_with_supplements(state)}
    ):
        parts.append(chunk.content or "")
    answer = "".join(parts)
    debug.info(
        "← Finalize 完成 (LLM直接回答)",
        detail=f"answer_len={len(answer)}",
        result=f"elapsed={time.time() - t0:.2f}s",
    )
    # 方案c: final_answer 落库(LLM 直接回答)
    dialogue_log.log_event(
        _dialogue_sid(config),
        "final_answer",
        {
            "answer": answer,
            "citations": _final_citations(state),
            "clarify_rounds": state.clarify_rounds,
        },
    )
    return {"final_answer": answer}


# Node 8.5: HITL Degrade — HITL-4 工具连续失败降级询问


def hitl_degrade_node(state: AgentState, config: RunnableConfig = None) -> dict:
    """interrupt: 重试/跳过/终止. resume 值由 API normalize_resume 归一为
    'retry'/'skip'/'abort'.

    Args:
        state (AgentState): 图状态,读取 plan / current_step_index 定位
            连续失败的工具名.

    Returns:
        dict: 状态更新,分支语义——resume 为 retry → degrade_used +
            error_streak 清零 + error 清空 + replan_needed/replan_reason
            (回 replanner 重新规划重试),hitl_event(choice=retry);
            abort → degrade_used + final_answer(服务不可用中止文案),
            hitl_event(choice=abort);skip(默认) → degrade_used +
            error_streak 清零 + error 清空,hitl_event(choice=skip),
            继续后续步骤.
    """
    failed_tool = "未知工具"
    if state.plan and state.current_step_index < len(state.plan):
        failed_tool = state.plan[state.current_step_index].tool_name or "未知工具"

    choice = interrupt(
        {
            "type": "degrade_confirm",
            "failed_tool": failed_tool,
            "options": [
                {"value": "重试", "label": "重新规划调用"},
                {"value": "跳过", "label": "跳过并继续后续步骤"},
                {"value": "终止", "label": "结束本次咨询"},
            ],
            "message": DEGRADE_CONFIRM_MSG.format(failed_tool=failed_tool),
        }
    )
    choice = str(choice).strip() if choice else ""

    # H3: 子串匹配降级为纯指令词 fast-path(与 utils.is_command_word 共用),
    # 自由文本已由 API normalize_resume 经 LLM 语义归一("retry"/"skip"/"abort")
    cmd = is_command_word(choice) or ""
    # 方案c: degrade_confirm 用户决策落库(resume 消费路径, 用户选择已知处)
    dialogue_log.log_event(
        _dialogue_sid(config),
        "interrupt_confirm",
        {
            "type": "degrade_confirm",
            "question": DEGRADE_CONFIRM_MSG.format(failed_tool=failed_tool),
            "chosen": (
                "重新规划调用"
                if cmd == "retry"
                else "结束本次咨询"
                if cmd in ("abort", "finish")
                else "跳过并继续后续步骤"
            ),
        },
    )
    # L15: 降级询问由一次性 degrade_used 改计数 —— 每询问一次门槛翻倍
    # (路由按 error_streak >= 阈值*(degrade_ask_count+1) 判定),
    # 允许再次询问但越来越难, 不再首次询问后永久关闭
    ask_count = state.degrade_ask_count + 1
    if cmd == "retry":
        debug.info("← Degrade: 用户选择重试", detail=f"tool={failed_tool}")
        return {
            "degrade_used": True,
            "degrade_ask_count": ask_count,
            "error_streak": 0,
            "error": None,
            "replan_needed": True,
            "replan_reason": f"用户要求重试失败的服务调用({failed_tool})",
            "hitl_event": {
                "type": "degrade_confirm",
                "choice": "retry",
                "at": datetime.now().isoformat(),
            },
        }
    if cmd in ("abort", "finish"):  # 终止类指令(结束/终止/stop)
        return {
            "degrade_used": True,
            "degrade_ask_count": ask_count,
            "final_answer": (
                "本次咨询因服务暂时不可用而中止,已收集的信息不会丢失。"
                "请稍后再试,或联系专业律师获取帮助。"
            ),
            "hitl_event": {
                "type": "degrade_confirm",
                "choice": "abort",
                "at": datetime.now().isoformat(),
            },
        }
    # skip(默认)
    debug.info("← Degrade: 用户选择跳过", detail=f"tool={failed_tool}")
    return {
        "degrade_used": True,
        "degrade_ask_count": ask_count,
        "error_streak": 0,
        "error": None,
        "hitl_event": {
            "type": "degrade_confirm",
            "choice": "skip",
            "at": datetime.now().isoformat(),
        },
    }


# Node 8.6: HITL Budget — HITL-6 重规划预算耗尽询问


def hitl_budget_node(state: AgentState, config: RunnableConfig = None) -> dict:
    """interrupt: 补充(原文) / 收尾(finish). resume 值经 normalize_resume:
    空或纯收尾指令词 → 'finish'; 其余非空文本 → 原文.

    Args:
        state (AgentState): 图状态,读取 evaluation / rag_documents / error /
            query 拼装缺失信息摘要.

    Returns:
        dict: 状态更新,分支语义——resume 为收尾(空或含收尾关键词) →
            budget_hitl_used + hitl_event(choice=finish),带现有材料
            finalize;补充原文 → budget_hitl_used + user_supplements 追加
            (M8: query 不改写) + replan_needed/replan_reason(最后一次
            执行) + error 清空,hitl_event(choice=supplement).
    """
    missing_parts = []
    if state.evaluation and state.evaluation.total > 0:
        ev = state.evaluation
        missing_parts.append(
            f"案例质量评估为「{ev.quality_verdict}」"
            f"(高质量{ev.correct_count}条/中等{ev.ambiguous_count}条)"
        )
    if not state.rag_documents:
        missing_parts.append("尚未检索到相关案例")
    if state.error:
        missing_parts.append(f"执行中出现错误: {state.error[:80]}")
    missing = "; ".join(missing_parts) or "信息仍不充分"

    answer = interrupt(
        {
            "type": "budget_confirm",
            "missing": missing,
            "options": [
                {"value": "补充", "label": "补充信息继续深入"},
                {"value": "收尾", "label": "基于现有材料收尾"},
            ],
            "message": BUDGET_CONFIRM_MSG.format(missing=missing),
        }
    )
    answer = str(answer).strip() if answer else ""

    # 方案c: budget_confirm 用户决策落库(resume 消费路径, 用户选择已知处)
    dialogue_log.log_event(
        _dialogue_sid(config),
        "interrupt_confirm",
        {
            "type": "budget_confirm",
            "question": BUDGET_CONFIRM_MSG.format(missing=missing),
            "chosen": (
                "基于现有材料收尾"
                if (not answer or is_command_word(answer) == "finish")
                else answer
            ),
        },
    )

    # H3: 子串匹配降级为纯指令词 fast-path ——「婚姻关系已于2020年结束」
    # 这类正常补充不再被误判成收尾;自由文本走补充分支
    if not answer or is_command_word(answer) == "finish":
        debug.info("← Budget: 用户选择收尾", detail="带现有材料 finalize")
        return {
            "budget_hitl_used": True,
            "hitl_event": {
                "type": "budget_confirm",
                "choice": "finish",
                "at": datetime.now().isoformat(),
            },
        }

    # M8: 补充进 user_supplements(query 不改写), 最后一次 replan 时由
    # _query_with_supplements 拼进 planner/replanner prompt
    debug.info("← Budget: 用户补充", detail=f"answer={answer[:80]} | 最后一次 replan")
    return {
        "budget_hitl_used": True,
        "user_supplements": [*(getattr(state, "user_supplements", None) or []), answer],
        "replan_needed": True,
        "replan_reason": "预算耗尽,用户补充关键信息,最后一次执行",
        "error": None,
        "hitl_event": {
            "type": "budget_confirm",
            "choice": "supplement",
            "at": datetime.now().isoformat(),
        },
    }


# 条件路由函数 (Conditional Edges)
# 路由为 state 的纯函数: 只读状态返回节点名,不产生副作用;build_graph 只负责装配.


def route_after_risk_gate(state: AgentState) -> str:
    """风险门控出口路由。

    Returns:
        str: 下一节点名 —— 已有 final_answer(高风险被拒,热线中止文案已写)
            → "finalize";否则(放行) → "element_assess".
    """
    if state.final_answer:
        return "finalize"
    return "element_assess"


def route_after_assess(state: AgentState) -> str:
    """要素评估出口路由。

    Returns:
        str: 下一节点名 —— 闲聊类 → "chitchat"(直接出轻量终答);
            有关键缺口反问且未达轮数上限 → "ask_element";
            否则(要素齐/轮数尽/软放行) → "planner".
    """
    if state.question_category == "chitchat":
        return "chitchat"
    if state.pending_questions and state.clarify_rounds < settings.max_clarify_rounds:
        return "ask_element"
    return "planner"


def route_after_ask(state: AgentState) -> str:
    """要素反问出口路由。

    Returns:
        str: 下一节点名 —— 轮数耗尽 → "planner"(软放行,按原问题继续);
            否则 → "element_assess"(应用用户回答后重新评估).
    """
    if state.clarify_rounds >= settings.max_clarify_rounds:
        return "planner"
    return "element_assess"


# planner 出口: 有计划 → executor 开始执行检索闭环
def route_after_planner(state: AgentState) -> str:
    """规划器出口路由。

    Returns:
        str: 下一节点名 —— 计划有步骤 → "executor";计划为空(闲聊/直答)
            → "finalize".
    """
    target = "executor" if state.plan else "finalize"
    debug.debug(f"路由: Planner → {target}", detail=f"plan_steps={len(state.plan)}")
    return target


# executor 出口: 带 tool_calls → tools; 连续失败达阈值 → hitl_degrade; 其余按剩余步骤走
def route_after_executor(state: AgentState) -> str:
    """执行器出口路由(含降级分支,L15 计数门槛)。

    Returns:
        str: 下一节点名 —— 连续失败达阈值*(degrade_ask_count+1) →
            "hitl_degrade"(每询问一次门槛翻倍, 不再一次性永久关闭);
            最新 AIMessage 带 tool_calls → "tools";其余按剩余步骤 →
            "executor"(还有步骤) / "replan_check"(全部完成).
    """
    # L15: 旧条件 ... and not state.degrade_used 一次询问后永久关闭
    if (
        state.error_streak
        >= settings.error_streak_threshold * (state.degrade_ask_count + 1)
    ):
        return "hitl_degrade"
    last_ai = next(
        (m for m in reversed(state.messages) if isinstance(m, AIMessage)), None
    )
    if last_ai is not None and getattr(last_ai, "tool_calls", None):
        return "tools"
    target = (
        "executor" if state.current_step_index < len(state.plan) else "replan_check"
    )
    debug.debug(
        f"路由: Executor → {target}",
        detail=f"step={state.current_step_index}/{len(state.plan)}",
    )
    return target


# merge 出口: 连续失败达阈值 → hitl_degrade; 其余按剩余步骤 → executor/replan_check
def route_after_merge(state: AgentState) -> str:
    """合并出口路由(含降级分支,L15 计数门槛)。

    Returns:
        str: 下一节点名 —— 连续失败达阈值*(degrade_ask_count+1) →
            "hitl_degrade"(每询问一次门槛翻倍, 不再一次性永久关闭);
            其余按剩余步骤 → "executor"(还有步骤) / "replan_check"(全部完成).
    """
    # L15: 旧条件 ... and not state.degrade_used 一次询问后永久关闭
    if (
        state.error_streak
        >= settings.error_streak_threshold * (state.degrade_ask_count + 1)
    ):
        return "hitl_degrade"
    target = (
        "executor" if state.current_step_index < len(state.plan) else "replan_check"
    )
    debug.debug(
        f"路由: Merge → {target}",
        detail=f"step={state.current_step_index}/{len(state.plan)}",
    )
    return target


# 质量门控出口(优先级短路): 通过→finalize > 预算→hitl_budget > vague未问过→mid_clarify > replanner
def route_after_replan_check(state: AgentState) -> str:
    """质量门控出口路由(优先级短路,见 spec §5.2）。

    Returns:
        str: 下一节点名,优先级从高到低 —— 质量通过(不需重规划) →
            "finalize";预算耗尽(工具调用数达 settings.max_rounds) → 已问过
            budget 则 "finalize"、未问过 → "hitl_budget";不足原因为
            vague(问题笼统)且未用过 → "mid_clarify";其余(not_found/
            error/已用过 mid) → "replanner".
    """
    executed = len(state.tool_calls)
    # 1. 质量通过
    if not state.replan_needed:
        return "finalize"
    # 2. 预算耗尽
    if executed >= settings.max_rounds:
        if state.budget_hitl_used:
            return "finalize"
        return "hitl_budget"
    # 3. 不足 · 笼统 · 未用过 mid_clarify
    if state.insufficient_reason == "vague" and not state.mid_clarify_used:
        return "mid_clarify"
    # 4. 其余(not_found/error/已用过 mid) → replanner
    return "replanner"


# 降级询问出口: abort(已写中止文案)→finalize / retry→replanner / skip(默认)→replan_check
def route_after_degrade(state: AgentState) -> str:
    """降级询问出口路由。

    Returns:
        str: 下一节点名 —— abort(final_answer 中止文案已写) → "finalize";
            retry(replan_needed 已置) → "replanner" 重新规划;
            skip(默认) → "replan_check" 质量门控收口.
    """
    if state.final_answer:
        return "finalize"
    if state.replan_needed:
        return "replanner"
    return "replan_check"


# replanner 出口(M3): 空补充计划 → finalize 收尾(材料不足);有新步骤 → executor
def route_after_replanner(state: AgentState) -> str:
    """重规划器出口路由(M3)。

    Returns:
        str: 下一节点名 —— replanner 产出空补充计划(replan_empty) →
            "finalize"(按材料不足收尾, 防 executor↔replan_check 空转
            到 recursion_limit);正常 → "executor"。
    """
    if state.replan_empty:
        return "finalize"
    return "executor"


# 预算询问出口: 选择补充(replan_needed 已置)→replanner 最后一搏 / 收尾(默认)→finalize
def route_after_budget(state: AgentState) -> str:
    """预算询问出口路由。

    Returns:
        str: 下一节点名 —— 补充(replan_needed 已置,最后一次执行) →
            "replanner";收尾(默认) → "finalize".
    """
    if state.replan_needed:
        return "replanner"
    return "finalize"


# 构建 Graph


def build_graph(checkpointer=None, store=None):
    """构建 Plan & Execute 主图(14 节点,子项目A 最终拓扑）。

    Args:
        checkpointer: LangGraph checkpointer(PostgresSaver / MemorySaver）,
            None 时不持久化(单次调用）
        store: LangGraph BaseStore(PostgresStore / InMemoryStore）,
            供记忆工具经 get_store() 访问
    """
    builder = StateGraph(AgentState)

    #  节点观测(决策 3): traced("node") 包装注册 — 记名称/时延/前后成果/结果
    #  (显式 name = 注册名: 函数名带 _node 后缀会与 values 回填的 pending_nodes 对不上)
    builder.add_node("ingest", traced("node", "ingest")(ingest_node))
    builder.add_node("risk_gate", traced("node", "risk_gate")(risk_gate_node))
    builder.add_node(
        "element_assess", traced("node", "element_assess")(element_assess_node)
    )
    builder.add_node("ask_element", traced("node", "ask_element")(ask_element_node))
    builder.add_node("planner", traced("node", "planner")(planner_node))
    builder.add_node("executor", traced("node", "executor")(executor_node))
    builder.add_node("tools", traced("node", "tools")(_build_tools_node()))
    builder.add_node("merge", traced("node", "merge")(merge_node))
    builder.add_node("replan_check", traced("node", "replan_check")(replan_check_node))
    builder.add_node("mid_clarify", traced("node", "mid_clarify")(mid_clarify_node))
    builder.add_node("hitl_degrade", traced("node", "hitl_degrade")(hitl_degrade_node))
    builder.add_node("hitl_budget", traced("node", "hitl_budget")(hitl_budget_node))
    builder.add_node("replanner", traced("node", "replanner")(replanner_node))
    builder.add_node("finalize", traced("node", "finalize")(finalize_node))
    builder.add_node("chitchat", traced("node", "chitchat")(chitchat_node))

    builder.add_edge(START, "ingest")
    builder.add_edge("ingest", "risk_gate")
    builder.add_conditional_edges(
        "risk_gate",
        route_after_risk_gate,
        {"element_assess": "element_assess", "finalize": "finalize"},
    )
    builder.add_conditional_edges(
        "element_assess",
        route_after_assess,
        {
            "ask_element": "ask_element",
            "planner": "planner",
            "chitchat": "chitchat",
        },
    )
    builder.add_conditional_edges(
        "ask_element",
        route_after_ask,
        {"element_assess": "element_assess", "planner": "planner"},
    )
    builder.add_conditional_edges(
        "planner",
        route_after_planner,
        {"executor": "executor", "finalize": "finalize"},
    )
    builder.add_conditional_edges(
        "executor",
        route_after_executor,
        {
            "tools": "tools",
            "executor": "executor",
            "replan_check": "replan_check",
            "hitl_degrade": "hitl_degrade",
        },
    )
    builder.add_edge("tools", "merge")
    builder.add_conditional_edges(
        "merge",
        route_after_merge,
        {
            "executor": "executor",
            "replan_check": "replan_check",
            "hitl_degrade": "hitl_degrade",
        },
    )
    builder.add_conditional_edges(
        "replan_check",
        route_after_replan_check,
        {
            "mid_clarify": "mid_clarify",
            "replanner": "replanner",
            "hitl_budget": "hitl_budget",
            "finalize": "finalize",
        },
    )
    builder.add_edge("mid_clarify", "replanner")
    builder.add_conditional_edges(
        "hitl_degrade",
        route_after_degrade,
        {
            "replanner": "replanner",
            "replan_check": "replan_check",
            "finalize": "finalize",
        },
    )
    builder.add_conditional_edges(
        "hitl_budget",
        route_after_budget,
        {"replanner": "replanner", "finalize": "finalize"},
    )
    # M3: replanner 出口改条件路由 —— 空补充计划(replan_empty)直接 finalize
    builder.add_conditional_edges(
        "replanner",
        route_after_replanner,
        {"executor": "executor", "finalize": "finalize"},
    )
    builder.add_edge("finalize", END)
    builder.add_edge("chitchat", END)

    return builder.compile(checkpointer=checkpointer, store=store)


# 模块级懒加载单例(简单场景直接 import graph；生产由 FastAPI lifespan 注入持久化版）

_graph = None


def get_graph():
    """默认图单例(无 checkpointer）。带持久化请用 FastAPI lifespan 或 build_graph()。"""
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


def set_graph(graph) -> None:
    """FastAPI lifespan 注入带持久化的图实例。"""
    global _graph
    _graph = graph
