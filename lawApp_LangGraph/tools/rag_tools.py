"""
RAG Agent 工具集
将 CRAG 流程拆解为 3 个 @tool,供 Plan & Execute Agent 自主调用

    retrieve_legal_knowledge → evaluate_case_relevance → analyze_legal_issue
                ↑                        ↑                       ↑
            检索(抽象后端)             三档质量评估           LLM 法律分析生成

v2 变更 (upgrade-v1):
- 检索走 RAG_service.base.get_retriever() 抽象层(pgvector 默认 / Pinecone 可选)
- 修正拼写: 检索评估字段名统一为 evaluate_retrieved_documents(旧拼写已全仓清除)
- LLM 懒加载单例,模块导入不再要求 API Key
- 移除全局 stream_queue;token 流由 graph.astream(stream_mode="messages") 驱动
"""

import threading
import time
from typing import Any, List, Optional

from langchain_core.tools import tool

from lawApp_LangGraph.FastAPI.logging import (
    tool as tool_log,
    rag as rag_log,
    system as sys_log,
)
from lawApp_LangGraph.prompts import get_analysis_prompt
from lawApp_LangGraph.config import settings
from lawApp_LangGraph.tracing import traced
from lawApp_LangGraph.state import (
    LawsResult,
    WebSearchResult,
    simpleRetrievedDocument,
    EvaluationResult,
    PromptsRecord,
)

#  LLM 懒加载单例 — analyze_legal_issue 用; Lock 双检防多线程重复初始化

_llm = None
_llm_lock = threading.Lock()


def _get_llm():
    """Flash LLM 懒加载（导入期不触碰 API Key）。"""
    global _llm
    if _llm is None:
        with _llm_lock:
            if _llm is None:
                from langchain_openai import ChatOpenAI

                _llm = ChatOpenAI(
                    model=settings.deepseek_flash_model,
                    openai_api_key=settings.deepseek_api_key,
                    openai_api_base=settings.deepseek_base_url,
                    temperature=0.4,
                    max_tokens=4096,
                )
    return _llm


# Tool 1: 案例库混合检索(向量+BM25) + CrossEncoder 重排; 后端异常降级返回 status=error 不中断流程
@tool
@traced("tool")
async def retrieve_legal_knowledge(
    query: str,
    top_k: int = 20,
    rerank_top_n: int = 5,
    alpha: float = 0.4,
    namespace: Optional[str] = None,
) -> dict:
    """从法律案例库中检索相关判例.支持混合检索(语义向量 + BM25 关键词匹配)与
    CrossEncoder 重排序,返回最相关的案例内容及其相关性评分.

    适用场景:
    - 查找与特定法律问题相关的历史判例
    - 获取类似案件的裁判要旨
    - 法律问题需要案例支撑时

    参数:
    query: 法律问题查询语句,中文
    top_k: 初始召回数量,最多不超过 50
    rerank_top_n: 重排序后返回数量,最多不超过 10 (从 top_k 中选出最相关的条目)
    alpha: 混合检索中的权重参数,默认 0.4 (越接近 1 越重视语义匹配;pgvector 后端忽略此项)
    namespace: 检索的命名空间,None 时用 settings.pinecone_namespace(env PINECONE_NAMESPACE)
    返回:
    结构化 dict,含 status / rag_documents 字段,
    每个文档为 RetrievedDocument 格式(case_number / case_cause / hybrid_score / chunk_text 等)
    """
    t0 = time.time()
    ns = namespace or settings.pinecone_namespace
    tool_log.info(
        "→ 调用工具: retrieve_legal_knowledge",
        detail=f"query={query[:60]} | top_k={top_k} | alpha={alpha} | ns={ns}",
    )

    try:
        from lawApp_LangGraph.RAG_service.base import get_retriever

        retriever = get_retriever()
        results = await retriever.search(
            query=query,
            top_k=top_k,
            rerank_top_n=rerank_top_n,
            alpha=alpha,
            namespace=ns,
        )
    except Exception as e:
        # 检索后端不可用时优雅降级,不中断 Agent 流程
        # M9: 异常原文不进工具结果(可含 DSN/密钥提示, LLM 可转述)——
        # 换固定中文文案, 原始异常进日志; 对齐 db_tools.fetch_laws 模式
        tool_log.error(
            "← 工具异常: retrieve_legal_knowledge",
            detail=f"检索后端不可用: {str(e)[:120]}",
            exc_info=True,
        )
        return {
            "status": "error",
            "message": "案例检索暂时不可用,请稍后重试",
            "rag_documents": [],
        }

    if not results:
        tool_log.info(
            "← 工具返回: retrieve_legal_knowledge",
            detail="未检索到相关案例",
            result=f"elapsed={time.time() - t0:.2f}s",
        )
        return {"status": "empty", "message": "未检索到相关案例", "rag_documents": []}

    top_score = results[0]["hybrid_score"] if results else 0
    tool_log.info(
        "← 工具返回: retrieve_legal_knowledge",
        detail=f"返回{len(results)}条案例 | top_score={top_score:.3f}",
        result=f"elapsed={time.time() - t0:.2f}s",
    )
    return {"status": "success", "count": len(results), "rag_documents": results}


# CRAG 三档评估(correct≥0.5 / ambiguous≥0.2 / incorrect), 产出 quality_verdict 判定是否需联网补充


# dict/对象统一转 simpleRetrievedDocument(评估与上下文拼装共用)
def _to_simple_doc(doc: Any) -> simpleRetrievedDocument:
    """将 dict 或对象转换为 simpleRetrievedDocument."""
    if isinstance(doc, simpleRetrievedDocument):
        return doc
    if isinstance(doc, dict):
        return simpleRetrievedDocument(
            id=doc.get("id", ""),
            year=str(doc.get("year", "")),
            case_number=doc.get("case_number", ""),
            case_cause=doc.get("case_cause", ""),
            chunk_text=doc.get("chunk_text", ""),
        )
    return simpleRetrievedDocument(
        id=getattr(doc, "id", ""),
        year=str(getattr(doc, "year", "")),
        case_number=getattr(doc, "case_number", ""),
        case_cause=getattr(doc, "case_cause", ""),
        chunk_text=getattr(doc, "chunk_text", ""),
    )


@tool
@traced("tool")
def evaluate_case_relevance(
    documents: list[dict[str, Any]],
) -> dict:
    """评估检索到的案例与用户问题的相关程度,按评分分为三档:
    - correct (高质量):     hybrid_score >= 0.5,可直接用于法律分析
    - ambiguous (中等质量): 0.2 <= score < 0.5,可参考但需谨慎
    - incorrect (低质量):   score < 0.2,不建议使用

    评估报告会明确告知检索质量是否「充足」或「不足,建议进行网络搜索补充」.
    Agent 应据此决定是否调用 get_google_search 进行联网补充.

    参数:
    documents: retrieve_legal_knowledge 返回结果中的 rag_documents 列表
    每项含 hybrid_score / chunk_text / case_number 等字段

    返回:
    结构化 dict,含 evaluation 键,其值为 correct/ambiguous/incorrect 分类及 quality_verdict
    """

    t0 = time.time()
    tool_log.info(
        "→ 调用工具: evaluate_case_relevance",
        detail=f"input_docs={len(documents) if documents else 0}",
    )

    if not documents:
        tool_log.info(
            "← 工具返回: evaluate_case_relevance",
            detail="输入为空",
            result="verdict=不足",
        )
        return {
            "evaluation": EvaluationResult(
                error="输入为空,没有可评估的文档",
                quality_verdict="不足,建议进行网络搜索补充",
            )
        }

    correct, ambiguous, incorrect = [], [], []

    for doc in documents:
        score = doc.get("hybrid_score", 0) if isinstance(doc, dict) else getattr(doc, "hybrid_score", 0)
        sdoc = _to_simple_doc(doc)
        if score >= settings.correct_threshold:
            correct.append(sdoc)
        elif score >= settings.incorrect_threshold:
            ambiguous.append(sdoc)
        else:
            incorrect.append(sdoc)

    total_usable = len(correct) + len(ambiguous)
    quality_verdict = (
        "充足"
        if len(correct) >= settings.min_quality_docs or total_usable >= settings.min_quality_docs
        else "不足,建议进行网络搜索补充"
    )

    tool_log.info(
        "← 工具返回: evaluate_case_relevance",
        detail=f"correct={len(correct)} | ambiguous={len(ambiguous)} | incorrect={len(incorrect)}",
        result=f"verdict={quality_verdict} | elapsed={time.time() - t0:.2f}s",
    )
    return {
        "evaluation": EvaluationResult(
            total=len(documents),
            correct_count=len(correct),
            ambiguous_count=len(ambiguous),
            incorrect_count=len(incorrect),
            quality_verdict=quality_verdict,
            correct=correct,
            ambiguous=ambiguous,
            incorrect=incorrect,
        )
    }


# Tool 3: 基于法条/案例/联网三源上下文流式生成法律分析(人设提示词在 prompts.get_analysis_prompt)


# dict/PromptsRecord/None 统一转 PromptsRecord
def _resolve_prompts_record(prompts_record: Any) -> PromptsRecord:
    """将 dict 或 PromptsRecord 统一转为 PromptsRecord,容错空值."""
    if prompts_record is None:
        return PromptsRecord()
    if isinstance(prompts_record, PromptsRecord):
        return prompts_record
    if isinstance(prompts_record, dict):
        return PromptsRecord(**prompts_record)
    return PromptsRecord()


# 拼装 PromptsRecord 中 已知要素+法条+案例+联网 的分析上下文块
def _build_analysis_context(pr: PromptsRecord) -> str:
    """从 PromptsRecord 的 web/law/case 字段拼装提示词上下文."""
    parts: list[str] = []
    if pr.known_elements and pr.known_elements != "暂无已知要素":
        parts.append(f"[已知案件要素]\n{pr.known_elements}")

    for law in pr.laws_results:
        parts.append(
            f"法条: {law.law_title}章节{law.chapter} 第{law.article_number}条\n{law.content}"
        )

    for doc in pr.evaluate_retrieved_documents:
        parts.append(
            f"[参考案例 | 案号:{doc.case_number} | {doc.year}年]\n案件内容:{doc.chunk_text}"
        )

    for item in pr.web_search_results:
        snippet = item.snippet if isinstance(item, WebSearchResult) else item.get("snippet", "")
        parts.append(f"[外部网络资料]\n{snippet}")

    return "\n\n---\n\n".join(parts) if parts else "暂无相关资料"


# 三源上下文喂 flash LLM 流式生成终稿分析, token 经 astream(messages) 透出
@tool
@traced("tool")
async def analyze_legal_issue(
    query: str,
    prompts_record: Any = None,
    **kwargs: Any,
) -> dict:
    """基于法律案例、法律条文和网络资料,生成专业的法律分析和建议.

    优先从 prompts_record (PromptsRecord) 中提取已累积的 web_search_results /
    evaluate_retrieved_documents / laws_results 来构建分析上下文.

    参数:
    query: 用户的法律问题
    prompts_record: Executor 累积的 PromptsRecord,由执行器自动注入,LLM 无需构造

    返回:
    结构化 dict,含 final_answer / final_prompt / prompts_record
    """
    t0 = time.time()

    pr = _resolve_prompts_record(prompts_record)

    case_n = len(pr.evaluate_retrieved_documents)
    web_n = len(pr.web_search_results)
    law_n = len(pr.laws_results)

    tool_log.info(
        "→ 调用工具: analyze_legal_issue",
        detail=f"query={query[:60]} | cases={case_n}条 | web={web_n}条 | law={law_n}条",
    )

    context = _build_analysis_context(pr)
    rag_log.debug("开始 LLM 法律分析生成", detail=f"context_len={len(context)}")

    llm = _get_llm()
    final_prompt = get_analysis_prompt().format(context=context, query=query)

    # 流式生成:token 经 langgraph astream(stream_mode="messages") 回调透出
    answer_parts: list[str] = []
    async for chunk in llm.astream(final_prompt):
        raw = chunk.content if hasattr(chunk, "content") else str(chunk)
        if isinstance(raw, list):
            raw = "".join(
                item.get("text", "") if isinstance(item, dict) else str(item)
                for item in raw
            )
        answer_parts.append(str(raw))

    answer = "".join(answer_parts)

    # M10: LLM 流读尽但 answer 为空 —— 不再当成功返回空 final_answer
    # (下游 finalize 复用空串会直接给出"空答案"); 显式 error 触发质量门控
    if not answer.strip():
        tool_log.warning("← 工具返回: analyze_legal_issue", result="answer 为空, 记 error")
        return {
            "status": "error",
            "message": "分析结果为空,请重试",
        }

    tool_log.info(
        "← 工具返回: analyze_legal_issue",
        detail=f"laws={law_n} | cases={case_n} | web={web_n}",
        result=f"answer_len={len(answer)} | elapsed={time.time() - t0:.2f}s",
    )

    return {
        "final_answer": answer,
        "final_prompts": final_prompt,
        "prompts_record": pr,
    }
