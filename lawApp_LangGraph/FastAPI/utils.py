from __future__ import annotations

import json
import re
import uuid
from typing import Optional

from fastapi import HTTPException

from lawApp_LangGraph.FastAPI.model import QueryResponse, SourceInfo
from lawApp_LangGraph.config import settings


#  工具函数


#  模式 → 会话 id 前缀(前缀-uuid 格式)
_SESSION_PREFIX = {"attorney": "AT", "assistant": "AS"}

#  合法会话 id 格式(M6):
#    新式  {AT|AS}-<uuid4hex12>          — uuid 跨进程/重启不撞号
#    旧式  {AT|AS}-YYYYMMDD-HHMMSS-NNN  — 存量会话兼容(时间戳式)
_SESSION_ID_NEW_RE = re.compile(r"^(AT|AS)-[0-9a-f]{12}$")
_SESSION_ID_OLD_RE = re.compile(r"^(AT|AS)-\d{8}-\d{6}-\d{1,6}$")


def new_session_id(mode: str = "attorney") -> str:
    """生成「前缀-uuid」格式的会话 id.

    Args:
        mode: 咨询模式("attorney"/"assistant"), 映射前缀 AT/AS.

    Returns:
        形如 "AT-1a2b3c4d5e6f" 的 id。旧实现(进程内计数器+秒级时间戳)
        在多 worker / 同秒重启时撞号, 撞号即 checkpoint 互串 → 改 uuid4。
    """
    prefix = _SESSION_PREFIX.get(mode, "AT")
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _validate_session_id(sid: str, mode: str) -> None:
    """显式传入 sid 的格式/前缀校验(M6/M13)。

    带 AT-/AS- 前缀的 sid 必须匹配新 uuid 或旧时间戳格式, 否则
    400 invalid_session_id;格式合法但前缀与端点模式不符 →
    400 session_mode_mismatch(双模式共用 sid 会串线程)。
    不带 AT-/AS- 前缀的历史 sid 原样放行(兼容, 不做模式校验)。
    """
    if _SESSION_ID_NEW_RE.match(sid) or _SESSION_ID_OLD_RE.match(sid):
        if not sid.startswith(f"{_SESSION_PREFIX.get(mode, 'AT')}-"):
            raise HTTPException(status_code=400, detail="session_mode_mismatch")
        return
    if sid.startswith(("AT-", "AS-")):
        # 带模式前缀但格式非法 → 拒绝
        raise HTTPException(status_code=400, detail="invalid_session_id")
    # 无前缀历史 sid: 放行


def ensure_session(session_id: Optional[str], mode: str = "attorney") -> str:
    """缺省时按模式生成新会话 id; 显式传入(续聊/恢复)则校验格式/前缀后透传."""
    sid = (session_id or "").strip()
    if not sid:
        return new_session_id(mode)
    _validate_session_id(sid, mode)
    return sid


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


_YES = ("y", "yes", "是", "确认", "好", "继续", "生成", "确认生成", "没问题")
_NO = ("n", "no", "否", "跳过", "不要", "中止", "算了", "先不要")

# HITL 短指令词集(H3 fast-path) — 仅当用户回复 strip 后与之**完全相等**
# 才直接命中; 子串匹配会把「婚姻关系已于2020年结束」这类正常补充误判成
# 收尾指令。值域: finish(收尾/结束)/ continue(继续)/ retry(重试)/
# abort(终止)/ skip(跳过)。
_COMMAND_WORDS = {
    # 英文(小写归一)
    "finish": "finish",
    "stop": "finish",
    "continue": "continue",
    "retry": "retry",
    "abort": "abort",
    "skip": "skip",
    # 中文
    "收尾": "finish",
    "结束": "finish",
    "继续": "continue",
    "重试": "retry",
    "终止": "abort",
    "跳过": "skip",
}


def is_command_word(text: str) -> Optional[str]:
    """判断用户回复是否为纯短指令词(完全相等, 大小写不敏感).

    Args:
        text: 用户原始回复文本.

    Returns:
        命中时返回规范指令("finish"/"continue"/"retry"/"abort"/"skip"),
        否则 None。
    """
    w = (text or "").strip().lower()
    return _COMMAND_WORDS.get(w)


async def normalize_resume(
    interrupt_type: str, answer: str, request_text: str = ""
) -> object:
    """按 interrupt 类型归一用户回复(spec §7.2; v4: 确认类自由文本改 LLM 语义判断).

    Args:
        interrupt_type: interrupt 载荷的 type 标签,取值为
            risk_confirm / pdf_confirm / degrade_confirm /
            budget_confirm / clarify / mid_clarify。
        answer: 用户的原始回复文本(可能为空)。
        request_text: interrupt 载荷的确认文案/问题文本,语义判断的上下文。

    Returns:
        risk_confirm / pdf_confirm: bool。显式确认词/拒绝词直接映射;
            其他自由文本交 semantic_confirm(LLM 语义判断);
        degrade_confirm: "retry" / "skip" / "abort" 之一;纯指令词直接命中,
            其余自由文本走 semantic_confirm(同意继续=retry,否则=abort);
        budget_confirm: 空回复或纯收尾指令词返回 "finish",
            其余自由文本走 semantic_confirm(同意继续=原文透传,否则=finish);
        clarify / mid_clarify: 原文透传(空=跳过)。
    """
    from lawApp_LangGraph.LangGraph_lawApp import semantic_confirm

    ans = (answer or "").strip()
    lowered = ans.lower()

    if interrupt_type in ("risk_confirm", "pdf_confirm"):
        if lowered in _YES:
            return True
        if lowered in _NO:
            return False
        # 自由文本 → LLM 语义判断(用户决策: 不按关键词硬匹配)
        return await semantic_confirm(request_text or "确认请求", ans)

    if interrupt_type == "degrade_confirm":
        cmd = is_command_word(ans)
        if cmd == "retry":
            return "retry"
        if cmd == "abort":
            return "abort"
        if cmd == "skip":
            return "skip"
        # 自由文本 → LLM 语义判断: 同意继续(重试)=retry, 否则=abort
        proceed = await semantic_confirm(request_text or "服务调用失败确认", ans)
        return "retry" if proceed else "abort"

    if interrupt_type == "budget_confirm":
        # 空回复 → 默认收尾(既有语义保留)
        if not ans:
            return "finish"
        cmd = is_command_word(ans)
        if cmd in ("finish", "abort"):
            return "finish"
        if cmd == "continue":
            return ans  # 继续补充
        # 自由文本 → LLM 语义判断: 同意继续(补充)=原文透传, 否则=finish
        proceed = await semantic_confirm(request_text or "补充信息确认", ans)
        return ans if proceed else "finish"

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


# 工具结果摘要(JSON 记录用): 列表记条数, pydantic 摘要化, 其余截断保可读
def _summarize_output(output) -> Any:
    if output is None:
        return "无输出"
    if isinstance(output, dict):
        return {
            k: (
                f"{len(v)} 条"
                if isinstance(v, list)
                else _summarize_output(v)
            )
            for k, v in output.items()
            if k != "prompts_record"
        }
    if isinstance(output, list):
        return f"{len(output)} 条"
    if hasattr(output, "model_dump"):
        return _summarize_output(output.model_dump())
    return str(output)[:120]


def build_tool_usage(state: dict) -> dict:
    """按 {tool_name: [结果摘要, ...]} 汇总工具使用记录(用户决策 v4).

    Args:
        state (dict): 图状态 values 快照,读取 tool_calls(ToolCallRecord).

    Returns:
        dict: 工具名 → 该工具历次调用结果摘要列表。
    """
    usage: dict[str, list] = {}
    for tc in state.get("tool_calls", []) or []:
        name = _field(tc, "tool_name", "")
        if not name:
            continue
        usage.setdefault(name, []).append(_summarize_output(_field(tc, "output", None)))
    return usage


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
        tool_usage=build_tool_usage(state),
    )


def sse_event(event: str, data) -> str:
    """SSE 帧: data: {"event": E, "data": D}\n\n — D 保持原类型
    (str 原样, dict/list 结构化)。前端 sse.js 与 07 册按此契约解析
    e["data"] 直接取对象(如 reasoning 帧的 {"source","delta"})。
    """
    return f"data: {json.dumps({'event': event, 'data': data}, ensure_ascii=False)}\n\n"
