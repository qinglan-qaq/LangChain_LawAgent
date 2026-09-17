from __future__ import annotations

import json
import uuid
from typing import Optional

from lawApp_LangGraph.FastAPI.model import QueryResponse, SourceInfo
from lawApp_LangGraph.config import settings


#  工具函数


def ensure_session(session_id: Optional[str]) -> str:
    if not session_id or not session_id.strip():
        return uuid.uuid4().hex
    return session_id


def get_graph():
    """获取装配好的图实例(优先 runtime 注入的持久化版)."""
    import lawApp_LangGraph.LangGraph_lawApp as app

    return app.get_graph()


def graph_config(session_id: str) -> dict:
    return {
        "configurable": {"thread_id": session_id},
        "recursion_limit": settings.recursion_limit,
    }


def extract_interrupt(snapshot) -> Optional[dict]:
    """从图的 interrupt 状态快照中提取 HITL 请求(无则 None)。

    snapshot: graph.aget_state(config) 或 astream 事件里的 state。
    """
    interrupts = getattr(snapshot, "interrupts", None) or []
    for intr in interrupts:
        value = getattr(intr, "value", None)
        if isinstance(value, dict) and value.get("type"):
            return value
    return None


_YES = ("y", "yes", "是", "确认", "好", "继续")
_NO = ("n", "no", "否", "跳过", "不要")


def normalize_resume(interrupt_type: str, answer: str) -> object:
    """按 interrupt 类型归一用户回复(spec §7.2).

    Args:
        interrupt_type: interrupt 载荷的 type 标签,取值为
            risk_confirm / pdf_confirm / degrade_confirm /
            budget_confirm / clarify / mid_clarify。
        answer: 用户的原始回复文本(可能为空)。

    Returns:
        risk_confirm / pdf_confirm: bool,确认词 True / 拒绝词 False,
            未识别默认拒绝(保守);
        degrade_confirm: "retry" / "skip" / "abort" 之一,默认 skip;
        budget_confirm: 空回复或含收尾指令(收尾/结束/finish)返回
            "finish",否则补充原文透传;
        clarify / mid_clarify: 原文透传(空=跳过)。
    """
    ans = (answer or "").strip()
    lowered = ans.lower()

    if interrupt_type in ("risk_confirm", "pdf_confirm"):
        if lowered in _YES:
            return True
        if lowered in _NO:
            return False
        return bool(lowered in _YES)  # 未识别默认拒绝(保守)

    if interrupt_type == "degrade_confirm":
        if "重试" in ans or "retry" in lowered:
            return "retry"
        if "终止" in ans or "结束" in ans or "abort" in lowered:
            return "abort"
        return "skip"  # 默认跳过

    if interrupt_type == "budget_confirm":
        if not ans or any(w in lowered for w in ("收尾", "结束", "finish")):
            return "finish"
        return ans  # 补充原文

    # clarify / mid_clarify: 原文透传
    return ans


def _field(item, key: str, default: str = ""):
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def build_sources(state: dict) -> list[SourceInfo]:
    sources: list[SourceInfo] = []
    seen: set[str] = set()

    for doc in state.get("rag_documents", []) or []:
        cn = _field(doc, "case_number", "")
        yr = _field(doc, "year", "")
        txt = _field(doc, "chunk_text", "")
        key = f"{cn}-{yr}"
        if key not in seen and cn:
            seen.add(key)
            sources.append(SourceInfo(case_number=cn, year=yr, snippet=txt[:200]))

    for item in state.get("web_search_results", []) or []:
        sources.append(
            SourceInfo(
                title=_field(item, "title", ""),
                link=_field(item, "link", ""),
                snippet=_field(item, "snippet", "")[:200],
            )
        )
    return sources


def build_tool_calls(state: dict) -> list[str]:
    return [
        tc_name
        for tc in state.get("tool_calls", []) or []
        if (tc_name := _field(tc, "tool_name", ""))
    ]


def build_response(state: dict, session_id: str) -> QueryResponse:
    pr = state.get("prompts_record")
    prompts_record = (
        pr.model_dump(mode="json")
        if hasattr(pr, "model_dump")
        else (pr if isinstance(pr, dict) else {})
    )
    # 案件要素面板数据(子项目A 澄清循环);无 case_elements 时为空列表
    ce = state.get("case_elements")
    elements = [
        {"key": e.key, "label": e.label, "critical": e.critical,
         "status": e.status, "value": e.value}
        for e in (ce.elements if ce else [])
    ] if ce else []
    return QueryResponse(
        query=state.get("query", ""),
        session_id=session_id,
        final_answer=state.get("final_answer", ""),
        final_prompt=state.get("final_prompts", ""),
        sources=build_sources(state),
        tool_calls=build_tool_calls(state),
        reasoning=state.get("reasoning", []) or [],
        prompts_record=prompts_record,
        elements=elements,
    )


def sse_event(event: str, data) -> str:
    if not isinstance(data, str):
        data = json.dumps(data, ensure_ascii=False)
    return f"data: {json.dumps({'event': event, 'data': data}, ensure_ascii=False)}\n\n"
