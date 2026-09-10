"""
Plan & Execute Agent v2 — 法律咨询智能体 (LangGraph 1.x)

双 LLM 架构:
    llm_planner  (DeepSeek Pro)   → structured output 制定计划/重规划
    llm_executor (DeepSeek Flash) → bind_tools 逐步执行、调用工具

Graph 流程:
    START → ingest → clarify(HITL①②) → planner
              planner ──[plan 空]──→ finalize → END
              planner ──[有步骤]──→ executor ⇄ tools(ToolNode) → merge
                          ↑                │
                          └── replanner ← replan_check ←┘ (质量不足)

v2 变更 (upgrade-v1):
- 4 处「剥代码栅栏 + json.loads」全部改为 with_structured_output(Pydantic Schema)
- executor 手动 TOOL_BY_NAME 循环 → prebuilt ToolNode + bind_tools(单工具)
- 全节点 async（replan_check / replanner 由同步 .invoke 改为 await ainvoke）
- 删除 _TOOL_FALLBACK_ARGS 字典（structured 单次重试取代）
- 新增 ingest 节点：每轮请求重置累积字段（reducer + RESET 标记），
  多轮对话不再泄漏上一轮的检索/调用记录
- HITL 三处 interrupt() + Command(resume=...)：
    ① clarify 节点: 关键事实缺失反问（每轮最多一次）
    ② clarify 节点: 高风险话题确认
    ③ executor: markdown_to_pdf 执行前确认
- LLM 懒加载单例，模块导入不再要求 API Key（可安全冒烟测试）
- 持久化: checkpointer(PostgresSaver/MemorySaver) + store(PostgresStore/InMemoryStore)
  由 FastAPI lifespan 注入；记忆工具经 langgraph.config.get_store() 访问
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

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
    EvaluationResult,
    PlanStep,
    PromptsRecord,
    ToolCallRecord,
)
from lawApp_LangGraph.tools import ALL_TOOLS

load_dotenv()

#  Task 3: 提示词统一改用 prompts.py 单一来源.
# 文件内同名旧常量已改名 _LEGACY_*(文本原样保留,Task 6 重构时删除),
# 否则下方旧赋值会遮蔽本 import,节点将拿不到 v2 提示词.
from lawApp_LangGraph.prompts import (
    EXECUTOR_PROMPT,
    FINALIZE_CASE_PROMPT,
    FINALIZE_DIRECT_PROMPT,
    PLANNER_SYSTEM,
    REPLAN_CHECK_PROMPT,
    REPLANNER_SYSTEM_PROMPT,
)
from lawApp_LangGraph.tools.rag_tools import analyze_legal_issue  # noqa — 已有,确认不缺

#  LLM 懒加载单例 — 导入期不触碰 API Key

_llm_planner = None
_llm_executor = None


def get_planner_llm():
    """Pro LLM（规划/重规划，强推理）。测试可 monkeypatch 本函数。"""
    global _llm_planner
    if _llm_planner is None:
        from langchain_openai import ChatOpenAI

        _llm_planner = ChatOpenAI(
            model=os.getenv("DEEPSEEK_PRO_MODEL", "deepseek-reasoner"),
            temperature=0.4,
            max_tokens=4096,
            openai_api_key=os.getenv("DEEPSEEK_API_KEY"),
            openai_api_base=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        )
    return _llm_planner


def get_executor_llm():
    """Flash LLM（执行/质量门控，低成本低延迟）。测试可 monkeypatch 本函数。"""
    global _llm_executor
    if _llm_executor is None:
        from langchain_openai import ChatOpenAI

        _llm_executor = ChatOpenAI(
            model=os.getenv("DEEPSEEK_FLASH_MODEL", "deepseek-chat"),
            temperature=0.25,
            max_tokens=2048,
            openai_api_key=os.getenv("DEEPSEEK_API_KEY"),
            openai_api_base=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        )
    return _llm_executor


TOOL_BY_NAME: Dict[str, Any] = {t.name: t for t in ALL_TOOLS()}

# 工具返回 dict 中与 AgentState 同名的 key 将被 merge 节点合并
_STATE_KEYS = {
    "rag_documents",
    "evaluation",
    "final_answer",
    "final_prompts",
    "crag_context",
    "web_search_results",
    "pdf_path",
    "is_pdf_output",
    "memory_results",
    "memory_update",
    "law_results",
    "prompts_record",
}

MAX_ROUNDS = 10  # 工具调用总数上限，防无限重规划


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

    class ClarifySchema(BaseModel):
        need_clarification: bool = Field(
            description="问题是否缺少关键事实,需要先向用户反问"
        )
        question: str = Field(default="", description="需要向用户反问的问题,一句话")
        high_risk: bool = Field(
            default=False, description="问题是否涉及高风险话题(自伤/暴力/刑事等)"
        )

    return PlanSchema, ReplanCheckSchema, ClarifySchema


PlanSchema, ReplanCheckSchema, ClarifySchema = _schema_models()


# Node 0: Ingest — 每轮请求入口,重置累积字段


def ingest_node(state: AgentState) -> dict:
    """重置上一轮遗留的计划/结果/累积字段（messages 保留，支撑多轮对话）。"""
    debug.debug("→ 进入 Ingest 节点", detail=f"query={state.query[:60]}")
    return {
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
        "crag_context": EvaluationResult(),
        "memory_results": [],
        "memory_update": None,
        "is_pdf_output": False,
        "pdf_path": None,
        "error": None,
        "clarification_round": 0,
        "clarification": None,
        "risk_confirmed": False,
        "pdf_confirmed": False,
        "hitl_event": None,
        # 累积语义字段 → RESET 清空
        "tool_calls": RESET,
        "reasoning": RESET,
        "web_search_results": RESET,
        "law_results": RESET,
    }


# Node 0.5: Clarify — HITL ①关键事实反问 + ②高风险话题确认

CLARIFY_PROMPT = """你是法律AI系统的接诊助理.判断用户的咨询是否需要先补充关键事实,或是否涉及高风险话题.

## 判断标准
1. 问题缺少无法推断的关键事实(如:金额、时间、婚姻状态、是否已有诉讼),
   且没有这些事实无法给出有价值的法律分析 → 需要反问
2. 闲聊、问候、概念解释类问题 → 不需要反问
3. 高风险话题: 自伤自杀倾向、家庭暴力正在发生、扬言报复伤害他人、涉及刑事犯罪自首等 → high_risk

## 用户问题
{query}

## 输出
按给定 JSON Schema 判断."""


async def clarify_node(state: AgentState) -> dict:
    """HITL 入口：关键事实缺失时反问用户（每轮最多一次）；高风险话题需确认。"""
    t0 = time.time()
    query = state.query.strip()

    if not query or state.clarification_round > 0:
        return {}

    verdict = None
    try:
        chain = PromptTemplate.from_template(
            CLARIFY_PROMPT
        ) | get_executor_llm().with_structured_output(ClarifySchema)
        verdict = await chain.ainvoke({"query": query[:2000]})
    except Exception as e:
        debug.warning("Clarify LLM 失败,走规则兜底", detail=str(e)[:100])

    need_clarify = bool(verdict and verdict.need_clarification and verdict.question)
    high_risk = bool(verdict and verdict.high_risk)

    if not need_clarify and not high_risk:
        debug.debug(
            "← Clarify 通过",
            detail="无需反问",
            result=f"elapsed={time.time() - t0:.2f}s",
        )
        return {}

    # ② 高风险确认优先（先确认风险，再补事实）
    if high_risk and not state.risk_confirmed:
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
        # 用户确认继续 → 继续反问判断
        return {
            "risk_confirmed": True,
            "hitl_event": {
                "type": "risk_confirm",
                "confirmed": True,
                "at": datetime.now().isoformat(),
            },
        }

    # ① 关键事实反问（interrupt 暂停图,等待 Command(resume=用户回复)）
    answer = interrupt({"type": "clarify", "question": verdict.question})
    if not answer or not str(answer).strip():
        # 用户跳过反问 → 按原问题继续
        debug.info("← Clarify 用户未补充", detail="按原问题继续")
        return {"clarification_round": 1}

    answer = str(answer).strip()
    augmented_query = f"{query}\n[用户补充信息] {answer}"
    debug.info(
        "← Clarify 完成",
        detail=f"补充信息: {answer[:80]}",
        result=f"elapsed={time.time() - t0:.2f}s",
    )
    return {
        "clarification_round": 1,
        "clarification": {"question": verdict.question, "answer": answer},
        "query": augmented_query,
        "hitl_event": {
            "type": "clarify",
            "question": verdict.question,
            "at": datetime.now().isoformat(),
        },
    }


# Node 1: The Planner — Pro LLM 制定计划 + 思考链

#  旧版常量 — Task 6 删除;改名 _LEGACY_* 避免遮蔽顶部 prompts.py 导入
_LEGACY_PLANNER_SYSTEM = """你是法律AI系统的任务规划师.分析用户问题,制定可执行的步骤计划.

## 可用工具
{available_tools}

## 计划原则
- 法律问题: retrieve_legal_knowledge → evaluate_case_relevance → analyze_legal_issue
- 如需要引用具体法律条文作为依据: 在检索案例后插入 fetch_laws 获取相关法条原文
- 如评估结果为"不足": 插入 get_google_search 联网补充再分析
- 如用户提及之前讨论过的话题: 先用 search_memory 搜索历史记忆获取上下文
- 一般情况下,在生成最终回答后用 save_to_memory 保存
- 一般情况下不需要过多网络搜索,优先利用 RAG 检索到的案例;如案例不足再补充网络搜索
- 如用户要求输出 PDF 报告: 最后一步调用 markdown_to_pdf 生成 PDF 文件(执行前系统会请求用户确认)
- 简单闲聊: plan 为空数组 []
- tool_name 必须是上述列表中的名称,不需要工具则填写 null
- 计划步骤不超过 8 步

## 用户问题
{query}"""


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
            {"query": query[:3000], "available_tools": _tools_desc()}
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

#  旧版常量 — Task 6 删除;改名 _LEGACY_* 避免遮蔽顶部 prompts.py 导入
_LEGACY_EXECUTOR_PROMPT = """你是执行器,只做一件事:调用指定的工具.

当前步骤: {step_description}
指定工具: {tool_name}
用户问题: {user_query}

上下文数据:
- 已检索案例: {rag_summary}
- 案例评估: {eval_summary}
- 检索法条: {law_summary}
- 网络搜索: {web_summary}

规则:
1. 只调用 {tool_name},不要调用其他工具
2. 从上下文和用户问题中提取参数
3. 不要做推理,只需正确调用工具
4. 必须发起一次工具调用"""


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
    elif getattr(tool_msg, "status", None) == "error":
        step_status = "failed"
        updates["error"] = (
            f"工具 {step.tool_name} 执行失败: {str(tool_msg.content)[:200]}"
        )
        output = {"error": str(tool_msg.content)[:500]}
    else:
        try:
            content = tool_msg.content
            output = json.loads(content) if isinstance(content, str) else content
        except (json.JSONDecodeError, TypeError):
            output = {"raw": str(tool_msg.content)[:500]}

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

#  旧版常量 — Task 6 删除;改名 _LEGACY_* 避免遮蔽顶部 prompts.py 导入
_LEGACY_REPLAN_CHECK_PROMPT = """你是法律AI系统的质量审核员。检查已执行步骤的结果，判断当前信息是否足以生成高质量的法律回答。

## 用户原始问题
{user_query}

## 已执行步骤及结果
{executed_summary}

## 当前数据状态
- 检索到的案例数量: {doc_count}
- 案例质量评估: {quality_verdict}
- 网络搜索补充: {web_count} 条
- 法律条文检索: {law_count} 条
- 执行错误: {error_info}

## 判断标准
1. 如果已检索到相关案例且质量评估为"充足" → 不需要重规划
2. 如果检索结果为空或质量评估为"不足"，且尚未进行网络搜索 → 需要重规划（补充 get_google_search）
3. 如果执行中出现了无法恢复的错误 → 需要重规划
4. 如果已有 final_answer 或 analyze_legal_issue 已成功执行 → 不需要重规划
5. 如果已有足够案例且进行了法律分析 → 不需要重规划"""


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

    needs, reason = False, ""
    try:
        chain = PromptTemplate.from_template(
            REPLAN_CHECK_PROMPT
        ) | get_executor_llm().with_structured_output(ReplanCheckSchema)
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
    except Exception as e:
        debug.warning("Replan Check LLM 失败,降级为规则判断", detail=str(e)[:100])
        needs, reason = _fallback_replan_check(state)

    elapsed = time.time() - t0
    target = "Replanner" if needs else "Finalize"
    debug.info(
        f"← Replan Check: {'需要重规划' if needs else '质量通过'}",
        detail=reason,
        result=f"elapsed={elapsed:.2f}s | → {target}",
    )
    return {"replan_needed": needs, "replan_reason": reason or None}


def _fallback_replan_check(state: AgentState) -> tuple[bool, str]:
    """规则兜底判断 —— LLM 判断失败时使用"""
    if state.error:
        return True, f"执行异常: {state.error[:60]}"
    if (
        state.evaluation
        and state.evaluation.quality_verdict == "不足,建议进行网络搜索补充"
    ):
        executed = {tc.tool_name for tc in state.tool_calls}
        if "get_google_search" not in executed:
            return True, "检索质量不足,需补联网搜索"
    return False, "规则兜底: 无明显问题"


# Node 6: The Replanner — Pro LLM 补充计划

#  旧版常量 — Task 6 删除;改名 _LEGACY_* 避免遮蔽顶部 prompts.py 导入
_LEGACY_REPLANNER_SYSTEM_PROMPT = """你是任务规划师.基于已执行的步骤和当前结果,生成**补充步骤**.

## 已执行步骤
{executed_steps}

## 当前状态
- 案例数量: {doc_count}
- 评估结论: {quality}
- 网络搜索: {web_count} 条
- 法律条文: {law_count} 条
- 错误: {error}

## 重规划原因
{replan_reason}

## 可用工具
{available_tools}

## 用户问题
{user_query}

## 要求
只输出需要**新增**的步骤,不要重复已完成的步骤.新增步骤不超过 3 步.
下一个步骤编号从 {next_id} 开始."""


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

#  旧版常量 — Task 6 删除;改名 _LEGACY_* 避免遮蔽顶部 prompts.py 导入
_LEGACY_FINALIZE_CASE_PROMPT = PromptTemplate.from_template(
    "基于以下案例,简要回答用户问题.\n案例:\n{docs}\n\n问题: {query}\n\n法律建议:"
)

_LEGACY_FINALIZE_DIRECT_PROMPT = PromptTemplate.from_template(
    "你是经验丰富的法律AI助手,七成理智,二成细腻,一成傲娇,请根据你的知识回答用户问题.\n问题: {query}\n回答:"
)


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


# 条件路由函数 (Conditional Edges)


def route_after_clarify(state: AgentState) -> str:
    """已生成中止回答 → finalize；否则 → planner。"""
    if state.final_answer:
        return "finalize"
    return "planner"


def route_after_planner(state: AgentState) -> str:
    """有步骤 → executor | 无步骤 → finalize"""
    target = "executor" if state.plan else "finalize"
    debug.debug(f"路由: Planner → {target}", detail=f"plan_steps={len(state.plan)}")
    return target


def route_after_executor(state: AgentState) -> str:
    """最新 AIMessage 带 tool_calls → tools；否则（跳过/失败步骤已推进索引）
    按剩余步骤决定回 executor 或进 replan_check。"""
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
    """还有步骤 → executor | 全部完成 → replan_check"""
    target = (
        "executor" if state.current_step_index < len(state.plan) else "replan_check"
    )
    debug.debug(
        f"路由: Merge → {target}",
        detail=f"step={state.current_step_index}/{len(state.plan)}",
    )
    return target


def route_after_replan_check(state: AgentState) -> str:
    """需重规划 → replanner | 质量通过 → finalize；超过 MAX_ROUNDS 强制终止"""
    executed = len(state.tool_calls)
    if executed >= MAX_ROUNDS:
        debug.info(
            "路由: ReplanCheck → finalize (已达最大轮数)",
            detail=f"tool_calls={executed}/{MAX_ROUNDS}",
        )
        return "finalize"
    target = "replanner" if state.replan_needed else "finalize"
    debug.debug(
        f"路由: ReplanCheck → {target}",
        detail=f"replan_needed={state.replan_needed} | executed={executed}",
    )
    return target


# 构建 Graph


def build_graph(checkpointer=None, store=None):
    """构建 Plan & Execute 主图。

    Args:
        checkpointer: LangGraph checkpointer（PostgresSaver / MemorySaver），
            None 时不持久化（单次调用）
        store: LangGraph BaseStore（PostgresStore / InMemoryStore），
            供记忆工具经 get_store() 访问
    """
    builder = StateGraph(AgentState)

    builder.add_node("ingest", ingest_node)
    builder.add_node("clarify", clarify_node)
    builder.add_node("planner", planner_node)
    builder.add_node("executor", executor_node)
    builder.add_node("tools", _build_tools_node())
    builder.add_node("merge", merge_node)
    builder.add_node("replan_check", replan_check_node)
    builder.add_node("replanner", replanner_node)
    builder.add_node("finalize", finalize_node)

    builder.add_edge(START, "ingest")
    builder.add_edge("ingest", "clarify")
    builder.add_conditional_edges(
        "clarify", route_after_clarify, {"planner": "planner", "finalize": "finalize"}
    )
    builder.add_conditional_edges(
        "planner", route_after_planner, {"executor": "executor", "finalize": "finalize"}
    )
    builder.add_conditional_edges(
        "executor",
        route_after_executor,
        {"tools": "tools", "executor": "executor", "replan_check": "replan_check"},
    )
    builder.add_edge("tools", "merge")
    builder.add_conditional_edges(
        "merge",
        route_after_merge,
        {"executor": "executor", "replan_check": "replan_check"},
    )
    builder.add_conditional_edges(
        "replan_check",
        route_after_replan_check,
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
