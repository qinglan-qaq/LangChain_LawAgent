"""
Plan & Execute Agent v2 — 法律咨询智能体 (LangGraph 1.x)

双 LLM 架构:
    llm_planner  (DeepSeek Pro)   → structured output 制定计划/重规划
    llm_executor (DeepSeek Flash) → bind_tools 逐步执行、调用工具

Graph 流程 (14 节点):
    START → ingest → risk_gate(HITL① 高风险确认) → element_assess
    element_assess ⇄ ask_element(HITL② 要素反问, 最多 settings.max_clarify_rounds 轮)
    element_assess ──[要素齐/轮数尽]──→ planner
    planner ──[plan 空]──→ finalize → END
    planner ──[有步骤]──→ executor ⇄ tools(ToolNode) → merge
    executor/merge ──[连续失败≥阈值]──→ hitl_degrade(HITL④)
        hitl_degrade ──[retry→replanner | skip→replan_check | abort→finalize]
    executor/merge ──[步骤完成]──→ replan_check
        replan_check ──[质量通过]──→ finalize
        replan_check ──[预算耗尽]──→ hitl_budget(HITL⑥)
            hitl_budget ──[补充→replanner | 收尾→finalize]
        replan_check ──[vague 未问过]──→ mid_clarify(HITL⑤) → replanner
        replan_check ──[其余]──→ replanner → executor

v2 变更 (upgrade-v1):
- 4 处「剥代码栅栏 + json.loads」全部改为 with_structured_output(Pydantic Schema)
- executor 手动 TOOL_BY_NAME 循环 → prebuilt ToolNode + bind_tools(单工具)
- 全节点 async（replan_check / replanner 由同步 .invoke 改为 await ainvoke）
- 删除 _TOOL_FALLBACK_ARGS 字典（structured 单次重试取代）
- 新增 ingest 节点：每轮请求重置累积字段（reducer + RESET 标记），
  多轮对话不再泄漏上一轮的检索/调用记录
- HITL 六处 interrupt() + Command(resume=...)：
    ① risk_gate: 高风险话题确认(拒绝 → 热线文案中止)
    ② ask_element: 关键要素缺失反问(要素循环, 最多 settings.max_clarify_rounds 轮)
    ③ executor: markdown_to_pdf 执行前确认
    ④ hitl_degrade: 工具连续失败降级询问(重试/跳过/终止)
    ⑤ mid_clarify: 检索反馈追问(先问人后搜网)
    ⑥ hitl_budget: 重规划预算耗尽询问(补充/收尾)
- LLM 懒加载单例，模块导入不再要求 API Key（可安全冒烟测试）
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
from lawApp_LangGraph.tools import ALL_TOOLS

load_dotenv()

from lawApp_LangGraph.config import settings

#  Task 3: 提示词统一改用 prompts.py 单一来源(旧版常量已于 Task 6 删除).
from lawApp_LangGraph.prompts import (
    BUDGET_CONFIRM_MSG,
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
)
from lawApp_LangGraph.tools.rag_tools import analyze_legal_issue  # noqa — 已有,确认不缺

#  LLM 懒加载单例 — 导入期不触碰 API Key; Lock 双检防多线程/多 worker 重复初始化

_llm_planner = None
_llm_planner_lock = threading.Lock()
_llm_executor = None
_llm_executor_lock = threading.Lock()


def get_planner_llm():
    """Pro LLM（规划/重规划，强推理）。测试可 monkeypatch 本函数。"""
    global _llm_planner
    if _llm_planner is None:
        with _llm_planner_lock:
            if _llm_planner is None:
                from langchain_openai import ChatOpenAI

                _llm_planner = ChatOpenAI(
                    model=settings.deepseek_pro_model,
                    temperature=0.4,
                    max_tokens=4096,
                    openai_api_key=settings.deepseek_api_key,
                    openai_api_base=settings.deepseek_base_url,
                )
    return _llm_planner


def get_executor_llm():
    """Flash LLM（执行/质量门控，低成本低延迟）。测试可 monkeypatch 本函数。"""
    global _llm_executor
    if _llm_executor is None:
        with _llm_executor_lock:
            if _llm_executor is None:
                from langchain_openai import ChatOpenAI

                _llm_executor = ChatOpenAI(
                    model=settings.deepseek_flash_model,
                    temperature=0.25,
                    max_tokens=2048,
                    openai_api_key=settings.deepseek_api_key,
                    openai_api_base=settings.deepseek_base_url,
                )
    return _llm_executor


TOOL_BY_NAME: Dict[str, Any] = {t.name: t for t in ALL_TOOLS()}

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
        applicable: bool = Field(description="是否婚姻家事类咨询")
        element_updates: List[ElementUpdate] = Field(
            default_factory=list, description="用户上轮回答映射到的要素"
        )
        na_keys: List[str] = Field(default_factory=list, description="本案不涉及的要素")
        promote_keys: List[str] = Field(
            default_factory=list, description="按案由升关键的要素"
        )
        questions: List[ElementQuestion] = Field(
            default_factory=list, description="本轮反问,只问关键且缺失,最多3个"
        )
        done: bool = Field(description="要素已足够,无需再问")

    class MidClarifySchema(BaseModel):
        question: str = Field(description="一个聚焦追问,律师问诊语气,一句话")
        element_key: str = Field(default="", description="追问对应的要素 key")

    return (
        PlanSchema,
        ReplanCheckSchema,
        RiskSchema,
        ElementAssessmentSchema,
        MidClarifySchema,
    )


PlanSchema, ReplanCheckSchema, RiskSchema, ElementAssessmentSchema, MidClarifySchema = (
    _schema_models()
)


# Node 0: Ingest — 每轮请求入口,重置累积字段


def _build_elements(mode: str) -> CaseElements:
    """按模式构建要素集: attorney=婚姻家事要素(现状不动), assistant=文书要素。"""
    from lawApp_LangGraph.prompts import DOC_ELEMENT_DEFS

    if mode == "assistant":
        return CaseElements(
            elements=[
                CaseElement(key=k, label=l, critical=c)
                for k, l, c in DOC_ELEMENT_DEFS
            ]
        )
    return default_case_elements()


def ingest_node(state: AgentState) -> dict:
    """重置上一轮遗留的计划/结果/累积字段（messages 保留，支撑多轮对话）。"""
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
        "pending_questions": [],
        # 累积语义字段 → RESET 清空
        "tool_calls": RESET,
        "reasoning": RESET,
        "web_search_results": RESET,
        "law_results": RESET,
        "clarify_history": RESET,
    }


# Node 0.5a: Risk Gate — 高风险话题确认 (HITL ①)


async def risk_gate_node(state: AgentState) -> dict:
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
        chain = (
            PromptTemplate.from_template(RISK_GATE_PROMPT)
            | get_executor_llm().with_structured_output(RiskSchema, method="json_mode")
        )
        verdict = await chain.ainvoke({"query": query[:2000]})
        high_risk = bool(verdict.high_risk)
    except Exception as e:
        # LLM 失败 → 视为无风险放行(HITL 是增强项不是阻塞项)
        debug.warning("Risk Gate LLM 失败,放行", detail=str(e)[:100])

    if not high_risk:
        debug.debug("← Risk Gate 通过", result=f"elapsed={time.time() - t0:.2f}s")
        return {}

    confirmed = interrupt(
        {
            "type": "risk_confirm",
            "message": "您的问题可能涉及人身安全或重大风险。如果您正面临家暴、自伤或紧迫的危险，请立即拨打110或联系当地妇联/救助机构。确认继续进行AI法律咨询吗？",
        }
    )
    if not confirmed:
        return {
            "final_answer": (
                "已中止本次咨询。请优先保证人身安全：紧急情况拨打110，"
                "家暴可拨打全国妇联维权热线12338，心理困境可拨打希望热线400-161-9995。"
                "安全得到保障后，欢迎随时回来咨询法律问题。"
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


async def element_assess_node(state: AgentState) -> dict:
    """评估案件要素:①应用用户上轮回答的要素映射 ②生成下一轮反问.

    Args:
        state (AgentState): 图状态,读取 query / case_elements /
            clarify_history / clarify_rounds.

    Returns:
        dict: 状态更新,键语义——case_elements: 深拷贝后应用要素更新
            (回答映射/na 标记/关键级提升)的清单,覆盖写回;
            pending_questions: 本轮反问列表,非空 → 路由继续反问,空 →
            放行进 planner. 分支:非婚姻家事类 → 全要素标 na 直通;
            LLM 失败 → pending_questions 置空,软放行不阻塞.
    """
    t0 = time.time()
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
        chain = (
            PromptTemplate.from_template(ELEMENT_ASSESS_PROMPT)
            | get_executor_llm().with_structured_output(ElementAssessmentSchema, method="json_mode")
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
        debug.warning(
            "Element Assess LLM 失败,软放行进 planner", detail=str(e)[:100]
        )
        return {"pending_questions": [], "case_elements": ce}

    # ① 非婚姻家事类 → 全 na,直接放行
    if not v.applicable:
        ce.mark_na([e.key for e in ce.elements])
        debug.info(
            "← Element Assess: 非目标类咨询,全 na 直通",
            result=f"elapsed={time.time() - t0:.2f}s",
        )
        return {"pending_questions": [], "case_elements": ce}

    # ② 应用要素更新(用户回答映射 + na + 关键级提升)
    valid_keys = {e.key for e in ce.elements}
    for u in v.element_updates:
        if u.key in valid_keys:
            ce.update(u.key, u.value, by="assess")
    if v.na_keys:
        ce.mark_na([k for k in v.na_keys if k in valid_keys])
    if v.promote_keys:
        ce.promote([k for k in v.promote_keys if k in valid_keys])

    # ③ 决定是否继续问
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
    return {"case_elements": ce, "pending_questions": questions}


# Node 0.5c: Ask Element — HITL ② 要素反问(纯记账,resume 重跑幂等)


def ask_element_node(state: AgentState) -> dict:
    """发起要素反问 interrupt;resume 后记录 clarify_history、轮数自增.

    纯记账节点不调 LLM;无 pending_questions 时直接返回,保证 resume
    重跑幂等. resume 返回值: 非空字符串=用户回答;空/None=跳过(轮数置满).

    Args:
        state (AgentState): 图状态,读取 pending_questions /
            case_elements / clarify_rounds / query.

    Returns:
        dict: 状态更新,各分支语义——无待问问题 → 空 dict 不重复
            interrupt;用户回答 → query 追加「[用户补充信息]」增强,
            clarify_rounds 自增,clarify_history 追加本轮
            ClarifyExchange,hitl_event(type=clarify, question=反问文本);
            用户跳过 → clarify_rounds 置满 settings.max_clarify_rounds 按原问题
            继续,hitl_event(type=clarify, skipped=True).
    """
    questions = state.pending_questions
    if not questions:
        return {}  # 防御:无问题不 interrupt

    question_text = " ".join(q.question for q in questions)
    keys = [q.key for q in questions]
    answer = interrupt(
        {
            "type": "clarify",
            "round": f"{state.clarify_rounds + 1}/{settings.max_clarify_rounds}",
            "question": question_text,
            "elements": [
                {"key": e.key, "label": e.label, "status": e.status}
                for e in state.case_elements.elements
            ],
        }
    )

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

    # 答案织入增强 query(下游 planner/executor/检索全部基于此)
    augmented_query = f"{state.query}\n[用户补充信息] {answer}"
    debug.info(
        "← Ask Element 完成",
        detail=f"answer={answer[:80]}",
        result=f"round={state.clarify_rounds + 1}/{settings.max_clarify_rounds}",
    )
    return {
        "clarify_rounds": state.clarify_rounds + 1,
        "clarify_history": [
            ClarifyExchange(
                round=state.clarify_rounds + 1,
                question=question_text,
                answer=answer,
                element_keys=keys,
            )
        ],
        "query": augmented_query,
        "hitl_event": {
            "type": "clarify",
            "question": question_text,
            "at": datetime.now().isoformat(),
        },
    }


# Node 1: The Planner — Pro LLM 制定计划 + 思考链


def _tools_desc() -> str:
    return "\n".join(f"- {t.name}: {(t.description or '')[:120]}" for t in ALL_TOOLS())


def _normalize_plan(schema) -> list[PlanStep]:
    """将 structured output 的计划规范为 PlanStep 列表（过滤未知工具名）。"""
    steps: list[PlanStep] = []
    for p in schema.plan:
        tn = p.tool_name
        if tn and tn not in TOOL_BY_NAME:
            tn = None
        steps.append(
            PlanStep(
                step_id=p.step_id or len(steps) + 1,
                description=p.description,
                tool_name=tn,
            )
        )
    return steps


async def planner_node(state: AgentState) -> dict:
    """Pro LLM: 分析问题 → structured 计划 + 思考链；失败降级为默认检索计划."""
    t0 = time.time()
    query = state.query.strip()
    debug.debug("→ 进入 Planner 节点", detail=f"query={query[:80]}")

    if not query:
        debug.info("← Planner 退出", detail="空输入", result="返回默认提示")
        return {
            "plan": [],
            "reasoning": ["无输入"],
            "final_answer": "抱一丝,你能再说一遍吗?",
        }

    try:
        chain = PromptTemplate.from_template(
            PLANNER_SYSTEM
        ) | get_planner_llm().with_structured_output(PlanSchema)
        result = await chain.ainvoke(
            {
                "query": query[:3000],
                "available_tools": _tools_desc(),
                "elements_digest": state.case_elements.digest(),
            }
        )
        plan = _normalize_plan(result)
        reasoning = list(result.reasoning or [])
    except Exception as e:
        debug.warning(
            "Planner structured output 失败,使用默认法律检索计划", detail=str(e)[:100]
        )
        return {
            "reasoning": [f"Planner 输出解析失败,使用默认法律检索计划: {str(e)[:80]}"],
            "plan": [
                PlanStep(
                    step_id=1,
                    description="检索相关法律案例",
                    tool_name="retrieve_legal_knowledge",
                ),
                PlanStep(
                    step_id=2,
                    description="评估检索质量",
                    tool_name="evaluate_case_relevance",
                ),
                PlanStep(step_id=3, description="检索法律条文", tool_name="fetch_laws"),
                PlanStep(
                    step_id=4,
                    description="综合信息生成法律分析",
                    tool_name="analyze_legal_issue",
                ),
            ],
        }

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


async def executor_node(state: AgentState) -> dict:
    """Flash LLM 为当前步骤生成工具调用（AIMessage + tool_calls）。

    - 无工具步骤 → 直接标记完成并推进
    - markdown_to_pdf 且未确认 → interrupt 请求用户确认（HITL ③）
    - LLM 未发起调用 → 严格重试一次；再失败则标记步骤 failed 并推进
    """
    t0 = time.time()
    idx = state.current_step_index
    plan = state.plan

    if idx >= len(plan):
        return {}

    step = plan[idx]
    total_steps = len(plan)
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

    # HITL ③: PDF 生成前确认（resume 后 confirmed 为真则继续）
    if step.tool_name == "markdown_to_pdf" and not state.pdf_confirmed:
        confirmed = interrupt(
            {
                "type": "pdf_confirm",
                "message": f"即将生成 PDF 报告（步骤: {step.description}）。确认生成吗？回复 y/是 确认，其他内容跳过该步骤。",
            }
        )
        if not confirmed or str(confirmed).strip().lower() in ("n", "no", "否", "跳过"):
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

    tool = TOOL_BY_NAME[step.tool_name]
    summaries = _step_summaries(state)
    prompt = EXECUTOR_PROMPT.format(
        step_description=step.description,
        tool_name=step.tool_name,
        user_query=state.query,
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
        # 严格重试一次（取代旧版 _TOOL_FALLBACK_ARGS 参数映射）
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


def _build_tools_node():
    return ToolNode(ALL_TOOLS(), handle_tool_errors=True)


# Node 4: Merge — 合并工具结果到 AgentState,推进步骤索引


def _field(item, key: str, default: str = ""):
    """从 dict 或 Pydantic 对象中安全提取字段值."""
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


async def merge_node(state: AgentState) -> dict:
    """解析 ToolMessage(dict 经 json.dumps 序列化)→ 合并到 state 字段,
    记录 ToolCallRecord,推进 current_step_index。"""
    t0 = time.time()
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


async def replan_check_node(state: AgentState) -> dict:
    """Flash LLM 语义判断是否需要重规划；失败降级为规则判断。"""
    t0 = time.time()
    debug.debug("→ 进入 Replan Check 节点", detail="LLM 语义判断执行质量...")

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
        chain = PromptTemplate.from_template(
            REPLAN_CHECK_PROMPT
        ) | get_executor_llm().with_structured_output(ReplanCheckSchema, method="json_mode")
        result = await chain.ainvoke(
            {
                "user_query": state.query[:1000],
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


# Node 5.5: Mid Clarify — HITL ⑤ 检索反馈追问(先问人后搜网)


async def mid_clarify_node(state: AgentState) -> dict:
    """基于检索结果的共同情形生成聚焦追问,先问人后搜网.

    Args:
        state (AgentState): 图状态,读取 query / rag_documents / case_elements.

    Returns:
        dict: 状态更新,分支语义——LLM 生成追问失败或用户未补充 →
            {"mid_clarify_used": True} 静默放行,由 replanner 联网兜底;
            用户补充 → query 织入「[检索反馈追问]/[用户澄清]」增强,
            case_elements 深拷贝后按 element_key 记录(来源 mid_clarify),
            附 hitl_event(type=mid_clarify, question=追问文本).
    """
    t0 = time.time()
    top_docs = "\n".join(
        f"- [{d.case_number}] {d.chunk_text[:120]}..."
        for d in state.rag_documents[:5]
    ) or "(检索为空)"

    try:
        chain = (
            PromptTemplate.from_template(MID_CLARIFY_PROMPT)
            | get_executor_llm().with_structured_output(MidClarifySchema, method="json_mode")
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

    answer = interrupt(
        {
            "type": "mid_clarify",
            "question": v.question,
            "context_hint": f"检索到的案例集中在: {top_docs[:200]}",
        }
    )
    answer = str(answer).strip() if answer else ""
    if not answer:
        debug.info("← Mid Clarify: 用户未补充", detail="转联网兜底")
        return {"mid_clarify_used": True}

    augmented_query = f"{state.query}\n[检索反馈追问] {v.question}\n[用户澄清] {answer}"
    ce = state.case_elements.model_copy(deep=True)
    if v.element_key in {e.key for e in ce.elements}:
        ce.update(v.element_key, answer, by="mid_clarify")
    debug.info(
        "← Mid Clarify 完成",
        detail=f"answer={answer[:80]}",
        result=f"elapsed={time.time() - t0:.2f}s | → replanner",
    )
    return {
        "mid_clarify_used": True,
        "query": augmented_query,
        "case_elements": ce,
        "hitl_event": {
            "type": "mid_clarify",
            "question": v.question,
            "at": datetime.now().isoformat(),
        },
    }


# Node 6: The Replanner — Pro LLM 补充计划


async def replanner_node(state: AgentState) -> dict:
    """Pro LLM: 生成补充计划 → 返回 Executor；解析失败降级为默认补充两步。"""
    t0 = time.time()
    reason = state.replan_reason or "质量不足"
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
        doc_count=len(state.rag_documents),
        quality=state.evaluation.quality_verdict if state.evaluation else "未评估",
        web_count=len(state.web_search_results),
        law_count=len(state.law_results),
        error=state.error or "无",
        replan_reason=reason,
        available_tools=_tools_desc(),
        user_query=state.query[:2000],
        next_id=len(state.plan) + 1,
    )

    try:
        chain = get_planner_llm().with_structured_output(PlanSchema)
        result = await chain.ainvoke(prompt)
        additional = [
            PlanStep(
                step_id=len(state.plan) + i + 1,
                description=p.description,
                tool_name=(p.tool_name if p.tool_name in TOOL_BY_NAME else None),
            )
            for i, p in enumerate(result.plan)
        ][:3]
        new_reasoning = [f"[Replan] {reason}"] + list(result.reasoning or [])
    except Exception as e:
        debug.warning(
            "Replanner structured output 失败,使用默认补充步骤", detail=str(e)[:100]
        )
        additional = [
            PlanStep(
                step_id=len(state.plan) + 1,
                description="联网搜索补充",
                tool_name="get_google_search",
            ),
            PlanStep(
                step_id=len(state.plan) + 2,
                description="综合信息生成分析",
                tool_name="analyze_legal_issue",
            ),
        ]
        new_reasoning = [f"[Replan 降级] {reason} → 插入默认补充步骤"]

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
    }


# Node 7: Finalize — 组装最终回答


async def finalize_node(state: AgentState) -> dict:
    """优先复用已有 final_answer；否则用案例兜底生成 / LLM 直接回答（流式）。"""
    t0 = time.time()
    debug.debug("→ 进入 Finalize 节点", detail="组装最终回答...")

    if state.final_answer:
        debug.info(
            "← Finalize 完成 (已有答案)",
            detail=f"answer_len={len(state.final_answer)}",
            result=f"elapsed={time.time() - t0:.2f}s",
        )
        return {}

    if state.rag_documents:
        docs = "\n".join(f"- {d.chunk_text[:300]}" for d in state.rag_documents[:3])
        chain = FINALIZE_CASE_PROMPT | get_executor_llm()
        parts: list[str] = []
        async for chunk in chain.astream({"docs": docs, "query": state.query}):
            parts.append(chunk.content or "")
        answer = "".join(parts)
        debug.info(
            "← Finalize 完成 (案例兜底)",
            detail=f"answer_len={len(answer)}",
            result=f"elapsed={time.time() - t0:.2f}s",
        )
        return {"final_answer": answer}

    chain = FINALIZE_DIRECT_PROMPT | get_executor_llm()
    parts = []
    async for chunk in chain.astream({"query": state.query}):
        parts.append(chunk.content or "")
    answer = "".join(parts)
    debug.info(
        "← Finalize 完成 (LLM直接回答)",
        detail=f"answer_len={len(answer)}",
        result=f"elapsed={time.time() - t0:.2f}s",
    )
    return {"final_answer": answer}


# Node 8.5: HITL Degrade — HITL ④ 工具连续失败降级询问


def hitl_degrade_node(state: AgentState) -> dict:
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
            "options": ["重试", "跳过", "终止"],
            "message": DEGRADE_CONFIRM_MSG.format(failed_tool=failed_tool),
        }
    )
    choice = str(choice).strip().lower() if choice else "skip"

    if "retry" in choice or "重试" in choice:
        debug.info("← Degrade: 用户选择重试", detail=f"tool={failed_tool}")
        return {
            "degrade_used": True,
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
    if "abort" in choice or "终止" in choice or "结束" in choice:
        return {
            "degrade_used": True,
            "final_answer": (
                "本次咨询因服务暂时不可用而中止，已收集的信息不会丢失。"
                "请稍后再试，或联系专业律师获取帮助。"
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
        "error_streak": 0,
        "error": None,
        "hitl_event": {
            "type": "degrade_confirm",
            "choice": "skip",
            "at": datetime.now().isoformat(),
        },
    }


# Node 8.6: HITL Budget — HITL ⑥ 重规划预算耗尽询问


def hitl_budget_node(state: AgentState) -> dict:
    """interrupt: 补充(原文) / 收尾(finish). resume 值经 normalize_resume:
    空或含收尾关键词 → 'finish'; 其余非空文本 → 原文.

    Args:
        state (AgentState): 图状态,读取 evaluation / rag_documents / error /
            query 拼装缺失信息摘要.

    Returns:
        dict: 状态更新,分支语义——resume 为收尾(空或含收尾关键词) →
            budget_hitl_used + hitl_event(choice=finish),带现有材料
            finalize;补充原文 → budget_hitl_used + query 织入
            「[用户补充信息]」+ replan_needed/replan_reason(最后一次
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
            "options": ["补充", "收尾"],
            "message": BUDGET_CONFIRM_MSG.format(missing=missing),
        }
    )
    answer = str(answer).strip() if answer else ""

    if not answer or any(w in answer.lower() for w in ("收尾", "结束", "finish")):
        debug.info("← Budget: 用户选择收尾", detail="带现有材料 finalize")
        return {
            "budget_hitl_used": True,
            "hitl_event": {
                "type": "budget_confirm",
                "choice": "finish",
                "at": datetime.now().isoformat(),
            },
        }

    augmented_query = f"{state.query}\n[用户补充信息] {answer}"
    debug.info(
        "← Budget: 用户补充", detail=f"answer={answer[:80]} | 最后一次 replan"
    )
    return {
        "budget_hitl_used": True,
        "query": augmented_query,
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
        str: 下一节点名 —— 有关键缺口反问且未达轮数上限 → "ask_element";
            否则(要素齐/轮数尽/软放行) → "planner".
    """
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


def route_after_planner(state: AgentState) -> str:
    """规划器出口路由。

    Returns:
        str: 下一节点名 —— 计划有步骤 → "executor";计划为空(闲聊/直答)
            → "finalize".
    """
    target = "executor" if state.plan else "finalize"
    debug.debug(f"路由: Planner → {target}", detail=f"plan_steps={len(state.plan)}")
    return target


def route_after_executor(state: AgentState) -> str:
    """执行器出口路由（含降级分支）。

    Returns:
        str: 下一节点名 —— 连续失败达阈值且未用过降级 → "hitl_degrade";
            最新 AIMessage 带 tool_calls → "tools";其余按剩余步骤 →
            "executor"(还有步骤) / "replan_check"(全部完成).
    """
    if state.error_streak >= settings.error_streak_threshold and not state.degrade_used:
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


def route_after_merge(state: AgentState) -> str:
    """合并出口路由（含降级分支）。

    Returns:
        str: 下一节点名 —— 连续失败达阈值且未用过降级 → "hitl_degrade";
            其余按剩余步骤 → "executor"(还有步骤) / "replan_check"(全部完成).
    """
    if state.error_streak >= settings.error_streak_threshold and not state.degrade_used:
        return "hitl_degrade"
    target = (
        "executor" if state.current_step_index < len(state.plan) else "replan_check"
    )
    debug.debug(
        f"路由: Merge → {target}",
        detail=f"step={state.current_step_index}/{len(state.plan)}",
    )
    return target


def route_after_replan_check(state: AgentState) -> str:
    """质量门控出口路由（优先级短路，见 spec §5.2）。

    Returns:
        str: 下一节点名，优先级从高到低 —— 质量通过(不需重规划) →
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
    """构建 Plan & Execute 主图（14 节点，子项目A 最终拓扑）。

    Args:
        checkpointer: LangGraph checkpointer（PostgresSaver / MemorySaver），
            None 时不持久化（单次调用）
        store: LangGraph BaseStore（PostgresStore / InMemoryStore），
            供记忆工具经 get_store() 访问
    """
    builder = StateGraph(AgentState)

    builder.add_node("ingest", ingest_node)
    builder.add_node("risk_gate", risk_gate_node)
    builder.add_node("element_assess", element_assess_node)
    builder.add_node("ask_element", ask_element_node)
    builder.add_node("planner", planner_node)
    builder.add_node("executor", executor_node)
    builder.add_node("tools", _build_tools_node())
    builder.add_node("merge", merge_node)
    builder.add_node("replan_check", replan_check_node)
    builder.add_node("mid_clarify", mid_clarify_node)
    builder.add_node("hitl_degrade", hitl_degrade_node)
    builder.add_node("hitl_budget", hitl_budget_node)
    builder.add_node("replanner", replanner_node)
    builder.add_node("finalize", finalize_node)

    builder.add_edge(START, "ingest")
    builder.add_edge("ingest", "risk_gate")
    builder.add_conditional_edges(
        "risk_gate", route_after_risk_gate,
        {"element_assess": "element_assess", "finalize": "finalize"},
    )
    builder.add_conditional_edges(
        "element_assess", route_after_assess,
        {"ask_element": "ask_element", "planner": "planner"},
    )
    builder.add_conditional_edges(
        "ask_element", route_after_ask,
        {"element_assess": "element_assess", "planner": "planner"},
    )
    builder.add_conditional_edges(
        "planner", route_after_planner,
        {"executor": "executor", "finalize": "finalize"},
    )
    builder.add_conditional_edges(
        "executor", route_after_executor,
        {"tools": "tools", "executor": "executor",
         "replan_check": "replan_check", "hitl_degrade": "hitl_degrade"},
    )
    builder.add_edge("tools", "merge")
    builder.add_conditional_edges(
        "merge", route_after_merge,
        {"executor": "executor", "replan_check": "replan_check",
         "hitl_degrade": "hitl_degrade"},
    )
    builder.add_conditional_edges(
        "replan_check", route_after_replan_check,
        {"mid_clarify": "mid_clarify", "replanner": "replanner",
         "hitl_budget": "hitl_budget", "finalize": "finalize"},
    )
    builder.add_edge("mid_clarify", "replanner")
    builder.add_conditional_edges(
        "hitl_degrade", route_after_degrade,
        {"replanner": "replanner", "replan_check": "replan_check",
         "finalize": "finalize"},
    )
    builder.add_conditional_edges(
        "hitl_budget", route_after_budget,
        {"replanner": "replanner", "finalize": "finalize"},
    )
    builder.add_edge("replanner", "executor")
    builder.add_edge("finalize", END)

    return builder.compile(checkpointer=checkpointer, store=store)


# 模块级懒加载单例（简单场景直接 import graph；生产由 FastAPI lifespan 注入持久化版）

_graph = None


def get_graph():
    """默认图单例（无 checkpointer）。带持久化请用 FastAPI lifespan 或 build_graph()。"""
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


def set_graph(graph) -> None:
    """FastAPI lifespan 注入带持久化的图实例。"""
    global _graph
    _graph = graph
