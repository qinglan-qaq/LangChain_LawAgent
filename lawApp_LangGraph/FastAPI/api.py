"""
Legal Consultation API v3.0.0 (upgrade-v1)

变更:
- 流式: /ask/stream 改由 graph.astream(stream_mode=["updates","messages","values"]) 驱动,
  移除全局 stream_queue 单例
- HITL: 新增 POST /ask/resume,interrupt 后用户回传 Command(resume=...)
- 持久化: lifespan 经 runtime.setup_runtime() 装配 PostgresSaver/PostgresStore
  (不可用时降级 InMemory),进程重启后同 thread_id 会话可续
- 审计: 引用来源 / HITL 事件写 audit 表(db.record_audit)
- 反馈: POST /feedback 记录用户评分
"""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from langgraph.types import Command

from lawApp_LangGraph.config import settings
from lawApp_LangGraph.FastAPI.logging import (
    flow,
    set_session,
    setup_logging,
    system,
)
from lawApp_LangGraph.FastAPI.model import (
    AssistantAskRequest,
    AttorneyAskRequest,
    FeedbackRequest,
    QueryRequest,
    QueryResponse,
    ResumeRequest,
    ToolInfo,
)
from lawApp_LangGraph.FastAPI.utils import (
    build_response,
    ensure_session,
    extract_interrupt,
    get_graph,
    graph_config,
    normalize_resume,
    sse_event,
)

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(
        log_dir=settings.log_dir,
        console_level=settings.log_console_level,
        file_level=settings.log_file_level,
    )
    from lawApp_LangGraph import runtime

    try:
        await runtime.setup_runtime()
        system.info(
            "系统启动",
            detail=f"checkpoint_backend={runtime.checkpoint_backend}",
            result="Legal Consultation API v3.0.0",
        )
    except Exception as e:  # pragma: no cover — runtime 内部已降级,此处兜底
        system.warning("运行时装配异常", detail=str(e)[:200])
    yield
    from lawApp_LangGraph import runtime

    await runtime.teardown_runtime()
    from lawApp_LangGraph.db import close_pool

    await close_pool()
    system.info("系统关闭", detail="服务已关闭")


app = FastAPI(
    title="Legal Consultation API",
    description="基于 LangGraph 1.x 的法律咨询后端接口 (Plan & Execute + HITL + 持久化)",
    version="3.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    t0 = time.time()
    response = await call_next(request)
    system.info(
        f"{request.method} {request.url.path}",
        detail=f"status={response.status_code}",
        result=f"elapsed={time.time() - t0:.3f}s",
    )
    return response


# ── 工具函数 ──


async def _finalize_or_interrupt(session_id: str, state_values) -> QueryResponse:
    """构建响应:若命中 interrupt 则附带请求体,并写审计."""
    graph = get_graph()
    config = graph_config(session_id)
    snapshot = await graph.aget_state(config)
    interrupt_req = extract_interrupt(snapshot)
    response = build_response(state_values, session_id)
    if interrupt_req:
        response.interrupt = interrupt_req
        await _safe_audit(session_id, "hitl_interrupt", interrupt_req)
    else:
        # 完成的回答:引用来源入审计
        await _safe_audit(
            session_id,
            "citations",
            {
                "tool_calls": response.tool_calls,
                "sources": [s.model_dump() for s in response.sources[:10]],
            },
        )
    return response


async def _safe_audit(session_id: str, event_type: str, payload: dict) -> None:
    try:
        from lawApp_LangGraph.db import record_audit, upsert_session

        await upsert_session(session_id)
        await record_audit(session_id, event_type, payload)
    except Exception:
        pass  # 审计失败不影响主流程(db 层已捕获,双保险)


async def _safe_upsert_session(sid: str) -> None:
    """会话登记(sessions 表);失败仅记日志, 不阻断咨询主流程。"""
    try:
        from lawApp_LangGraph.db import upsert_session

        await upsert_session(sid)
    except Exception as e:  # pragma: no cover
        system.warning("sessions 登记失败", detail=str(e)[:100])


# ── 端点 ──


@app.post("/ask", response_model=QueryResponse)
async def ask(request: QueryRequest):
    """(deprecated — 请改用 /attorney/ask|/assistant/ask, 二期移除)
    同步问答: 等待完整结果后返回 JSON;遇到 interrupt 返回 interrupt 字段."""
    sid = ensure_session(request.session_id)
    set_session(sid)
    query_preview = request.query[:80].replace("\n", " ")
    flow.info("流程开始", summary="用户提问", detail=f"query={query_preview}")

    graph = get_graph()
    config = graph_config(sid)
    t0 = time.time()
    try:
        state = await graph.ainvoke({"query": request.query}, config=config)
    except Exception as e:
        flow.error("流程异常", summary="Graph 执行失败", detail=str(e))
        raise HTTPException(status_code=500, detail=f"Graph 执行失败: {e}")

    elapsed = time.time() - t0
    response = await _finalize_or_interrupt(sid, state)
    flow.info(
        "流程结束",
        summary="回答生成完毕" if not response.interrupt else "等待用户回复(HITL)",
        detail=f"answer_len={len(response.final_answer)}, tool_calls={len(response.tool_calls)}",
        result=f"总耗时={elapsed:.2f}s | 工具: {', '.join(response.tool_calls) or '无'}",
    )
    return response


@app.post("/attorney/ask", response_model=QueryResponse)
async def attorney_ask(request: AttorneyAskRequest):
    """代理律师模式: 多轮追问案情 → 完整法律咨询答复(阻塞式)。"""
    sid = ensure_session(request.session_id)
    set_session(sid)
    await _safe_upsert_session(sid)
    flow.info("流程开始", summary="代理律师模式提问", detail=f"query={request.query[:80]}")
    graph = get_graph()
    t0 = time.time()
    try:
        state = await graph.ainvoke(
            {"query": request.query, "mode": "attorney"}, config=graph_config(sid)
        )
    except Exception as e:
        flow.error("流程异常", summary="Graph 执行失败", detail=str(e))
        raise HTTPException(status_code=500, detail=f"Graph 执行失败: {e}")

    elapsed = time.time() - t0
    response = await _finalize_or_interrupt(sid, state)
    flow.info(
        "流程结束",
        summary="回答生成完毕" if not response.interrupt else "等待用户回复(HITL)",
        detail=f"answer_len={len(response.final_answer)}, tool_calls={len(response.tool_calls)}",
        result=f"总耗时={elapsed:.2f}s | 工具: {', '.join(response.tool_calls) or '无'}",
    )
    return response


@app.post("/assistant/ask", response_model=QueryResponse)
async def assistant_ask(request: AssistantAskRequest):
    """律师助理模式: 完整案情 + 文书类型 → 起诉状/答辩状草稿(阻塞式)。"""
    sid = ensure_session(request.session_id)
    set_session(sid)
    await _safe_upsert_session(sid)
    doc_label = "起诉状" if request.doc_type == "complaint" else "答辩状"
    flow.info(
        "流程开始", summary=f"律师助理模式起草{doc_label}", detail=f"案情={request.case_details[:80]}"
    )
    graph = get_graph()
    t0 = time.time()
    try:
        state = await graph.ainvoke(
            {
                "query": request.case_details,
                "mode": "assistant",
                "doc_type": request.doc_type,
            },
            config=graph_config(sid),
        )
    except Exception as e:
        flow.error("流程异常", summary="Graph 执行失败", detail=str(e))
        raise HTTPException(status_code=500, detail=f"Graph 执行失败: {e}")

    elapsed = time.time() - t0
    response = await _finalize_or_interrupt(sid, state)
    flow.info(
        "流程结束",
        summary="文书起草完毕" if not response.interrupt else "等待用户回复(HITL)",
        detail=f"answer_len={len(response.final_answer)}, tool_calls={len(response.tool_calls)}",
        result=f"总耗时={elapsed:.2f}s | 工具: {', '.join(response.tool_calls) or '无'}",
    )
    return response


@app.get("/disclaimer")
async def disclaimer():
    """代理律师模式免责声明文本(前端小弹窗内容源, 非阻塞提示)。"""
    from lawApp_LangGraph.prompts import DISCLAIMER_TEXT

    return {"disclaimer": DISCLAIMER_TEXT}


@app.get("/sessions")
async def list_sessions():
    """会话列表(sessions 表, 双模式端点的 _safe_upsert_session 数据源)。"""
    from lawApp_LangGraph.db import get_pool

    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT session_id, meta, last_active_at FROM sessions "
            "ORDER BY last_active_at DESC LIMIT 50"
        )
        rows = await cur.fetchall()
    return [
        {"session_id": r[0], "meta": r[1], "last_active_at": str(r[2])} for r in rows
    ]


@app.get("/sessions/{sid}")
async def get_session(sid: str):
    """单会话详情: 最新快照 + interrupt 状态(等待回复时返回待回答问题)。"""
    graph = get_graph()
    snap = await graph.aget_state(graph_config(sid))
    if not snap or not snap.values:
        raise HTTPException(status_code=404, detail=f"会话 {sid} 不存在")
    response = build_response(snap.values, sid)
    return {**response.model_dump(), "interrupt": extract_interrupt(snap)}


@app.post("/ask/resume", response_model=QueryResponse)
async def ask_resume(request: ResumeRequest):
    """HITL 继续: 用户对 interrupt 的回复经 Command(resume=...) 回传,图从暂停点恢复."""
    sid = ensure_session(request.session_id)
    set_session(sid)
    flow.info("HITL 恢复", summary="用户回传", detail=f"answer={request.answer[:60]}")

    graph = get_graph()
    config = graph_config(sid)

    # 先读快照取 interrupt 类型,再类型感知归一
    snapshot = await graph.aget_state(config)
    interrupt_req = extract_interrupt(snapshot)
    itype = (interrupt_req or {}).get("type", "")
    resume_value = normalize_resume(itype, request.answer)

    t0 = time.time()
    try:
        state = await graph.ainvoke(Command(resume=resume_value), config=config)
    except Exception as e:
        flow.error("HITL 恢复失败", detail=str(e))
        raise HTTPException(status_code=500, detail=f"恢复失败: {e}")

    response = await _finalize_or_interrupt(sid, state)
    flow.info(
        "HITL 恢复完成",
        summary="回答生成完毕" if not response.interrupt else "等待用户回复(HITL)",
        result=f"总耗时={time.time() - t0:.2f}s",
    )
    return response


@app.get("/ask/stream")
async def ask_stream(query: str = "", session_id: str | None = None):
    """(deprecated — 请改用 /attorney/ask/stream|/assistant/ask/stream, 二期移除)
    SSE 流式问答: updates 驱动进度,messages 驱动 token 打字机,values 收尾."""
    if not query.strip():
        raise HTTPException(status_code=422, detail="query 不能为空")

    sid = ensure_session(session_id)
    set_session(sid)
    flow.info("流式流程开始", summary="用户提问", detail=f"query={query[:80]}")
    config = graph_config(sid)
    graph = get_graph()

    async def event_stream():
        final_state: dict = {}
        seen_steps: set[str] = set()
        try:
            async for stream_mode, chunk in graph.astream(
                {"query": query},
                config=config,
                stream_mode=["updates", "messages", "values"],
            ):
                if stream_mode == "messages":
                    # (message_chunk, metadata);只要带内容的增量 token
                    msg, meta = chunk
                    content = getattr(msg, "content", "")
                    if isinstance(content, str) and content.strip():
                        # 只透出工具/生成节点的 LLM token,跳过绑定工具的决策消息
                        if not getattr(msg, "tool_calls", None):
                            yield sse_event("token", content)
                elif stream_mode == "updates":
                    # chunk: {node_name: state_update_dict}
                    for node_name, updates in chunk.items():
                        if not isinstance(updates, dict):
                            continue
                        if node_name in ("planner", "replanner") and "plan" in updates:
                            for s in updates.get("plan") or []:
                                key = f"{s.step_id}:{s.tool_name}"
                                if key not in seen_steps:
                                    seen_steps.add(key)
                                    yield sse_event(
                                        "progress",
                                        f"[{s.step_id}/{len(updates['plan'])}] {s.description}",
                                    )
                        if node_name == "executor" and "plan" in updates:
                            plan = updates.get("plan") or []
                            idx = updates.get("current_step_index")
                            if idx is not None and idx < len(plan):
                                step = plan[idx]
                                yield sse_event("tool_call", step.tool_name or "无")
                        if node_name == "merge":
                            for k in (
                                "rag_documents",
                                "web_search_results",
                                "law_results",
                                "evaluation",
                            ):
                                if k in updates and updates[k]:
                                    n = (
                                        len(updates[k])
                                        if isinstance(updates[k], list)
                                        else 1
                                    )
                                    yield sse_event("tool_result", f"{k}: {n}")
                        if node_name == "element_assess" and "case_elements" in updates:
                            ce = updates.get("case_elements")
                            elems = getattr(ce, "elements", None) or []
                            yield sse_event(
                                "elements",
                                [
                                    {"key": e.key, "label": e.label, "status": e.status}
                                    for e in elems
                                ],
                            )
                elif stream_mode == "values":
                    final_state = chunk

            # 检查是否停在 interrupt
            snapshot = await graph.aget_state(config)
            interrupt_req = extract_interrupt(snapshot)
            if interrupt_req:
                yield sse_event("interrupt", interrupt_req)
                await _safe_audit(sid, "hitl_interrupt", interrupt_req)
            else:
                answer = final_state.get("final_answer", "")
                yield sse_event("answer", answer)
                yield sse_event("session_id", sid)

                pr = final_state.get("prompts_record")
                if pr is not None:
                    record_data = (
                        pr.model_dump(mode="json")
                        if hasattr(pr, "model_dump")
                        else (pr if isinstance(pr, dict) else {})
                    )
                    yield sse_event("prompts_record", record_data)
                if final_state.get("final_prompts"):
                    yield sse_event("final_prompts", final_state["final_prompts"])
                await _safe_audit(
                    sid,
                    "citations",
                    {"tool_calls": final_state.get("tool_calls", [])},
                )
            yield sse_event("done")
            flow.info(
                "流式流程结束",
                summary="流式回答完成",
                result=f"answer_len={len(final_state.get('final_answer', ''))}",
            )
        except Exception as e:
            flow.error("流式流程异常", detail=str(e))
            yield sse_event("error", str(e))

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _validate_stream_text(text: str, limit: int = 4000) -> None:
    """GET 流式端点的 URL 长度防护(用户决策: 直接报错不截断)。"""
    if len(text) > limit:
        raise HTTPException(
            status_code=413, detail=f"文本过长({len(text)} > {limit} 字), 请分批发送"
        )


async def _mode_stream(
    mode: str, query: str, doc_type: str, session_id: str | None
) -> StreamingResponse:
    """双模式 SSE 流式工厂: 旧 /ask/stream 全事件协议 + reasoning CoT 流。

    Args:
        mode: "attorney" 或 "assistant"。
        query: 提问/案情文本。
        doc_type: assistant 模式的文书类型(attorney 传空)。
        session_id: 续聊会话 ID。

    Returns:
        StreamingResponse(text/event-stream)。
    """
    if not query.strip():
        raise HTTPException(status_code=422, detail="query 不能为空")
    _validate_stream_text(query)
    sid = ensure_session(session_id)
    set_session(sid)
    await _safe_upsert_session(sid)
    config = graph_config(sid)
    graph = get_graph()
    from lawApp_LangGraph.LangGraph_lawApp import (
        close_reasoning_channel,
        open_reasoning_channel,
    )

    inputs = {"query": query, "mode": mode}
    if mode == "assistant":
        inputs["doc_type"] = doc_type

    async def event_stream():
        import asyncio

        out_q: asyncio.Queue = asyncio.Queue()
        reasoning_q = open_reasoning_channel(sid)

        async def pump():
            """把 CoT 总线增量搬进统一输出队列;排空后发 done 终止帧。"""
            while True:
                item = await reasoning_q.get()
                if item is None:
                    break
                await out_q.put(("reasoning", item))
            await out_q.put(("done", ""))
            await out_q.put(None)

        async def run():
            """消费 graph.astream, 事件形态与既有 /ask/stream 完全一致。"""
            final_state: dict = {}
            seen_steps: set[str] = set()
            try:
                async for stream_mode, chunk in graph.astream(
                    inputs,
                    config=config,
                    stream_mode=["updates", "messages", "values"],
                ):
                    if stream_mode == "messages":
                        msg, meta = chunk
                        content = getattr(msg, "content", "")
                        if (
                            isinstance(content, str)
                            and content.strip()
                            and not getattr(msg, "tool_calls", None)
                        ):
                            await out_q.put(("token", content))
                    elif stream_mode == "updates":
                        for node_name, updates in (chunk or {}).items():
                            if not isinstance(updates, dict):
                                continue
                            if node_name in ("planner", "replanner") and "plan" in updates:
                                for s in updates.get("plan") or []:
                                    key = f"{s.step_id}:{s.tool_name}"
                                    if key not in seen_steps:
                                        seen_steps.add(key)
                                        await out_q.put(
                                            (
                                                "progress",
                                                f"[{s.step_id}/{len(updates['plan'])}] {s.description}",
                                            )
                                        )
                            if node_name == "executor" and "plan" in updates:
                                plan = updates.get("plan") or []
                                idx = updates.get("current_step_index")
                                if idx is not None and idx < len(plan):
                                    step = plan[idx]
                                    await out_q.put(("tool_call", step.tool_name or "无"))
                            if node_name == "merge":
                                for k in (
                                    "rag_documents",
                                    "web_search_results",
                                    "law_results",
                                    "evaluation",
                                ):
                                    if k in updates and updates[k]:
                                        n = (
                                            len(updates[k])
                                            if isinstance(updates[k], list)
                                            else 1
                                        )
                                        await out_q.put(("tool_result", f"{k}: {n}"))
                            if node_name == "element_assess" and "case_elements" in updates:
                                ce = updates.get("case_elements")
                                elems = getattr(ce, "elements", None) or []
                                await out_q.put(
                                    (
                                        "elements",
                                        [
                                            {"key": e.key, "label": e.label, "status": e.status}
                                            for e in elems
                                        ],
                                    )
                                )
                    elif stream_mode == "values":
                        final_state = chunk or final_state

                snapshot = await graph.aget_state(config)
                interrupt_req = extract_interrupt(snapshot)
                if interrupt_req:
                    await out_q.put(("interrupt", interrupt_req))
                    await _safe_audit(sid, "hitl_interrupt", interrupt_req)
                else:
                    answer = (final_state.get("final_answer") or "").strip()
                    if answer:
                        await out_q.put(("answer", answer))
                    await out_q.put(("session_id", sid))

                    pr = final_state.get("prompts_record")
                    if pr is not None:
                        record_data = (
                            pr.model_dump(mode="json")
                            if hasattr(pr, "model_dump")
                            else (pr if isinstance(pr, dict) else {})
                        )
                        await out_q.put(("prompts_record", record_data))
                    if final_state.get("final_prompts"):
                        await out_q.put(("final_prompts", final_state["final_prompts"]))
                    await _safe_audit(
                        sid,
                        "citations",
                        {"tool_calls": final_state.get("tool_calls", [])},
                    )
            except Exception as e:
                flow.error("流式流程异常", detail=str(e))
                await out_q.put(("error", str(e)))
            finally:
                # 唤醒 pump 排空残余 CoT 增量, done 由 pump 统一发出
                await reasoning_q.put(None)

        tasks = [asyncio.create_task(run()), asyncio.create_task(pump())]
        try:
            while True:
                item = await out_q.get()
                if item is None:
                    break
                yield sse_event(item[0], item[1])
        finally:
            for t in tasks:
                t.cancel()
            close_reasoning_channel(sid)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/attorney/ask/stream")
async def attorney_ask_stream(query: str = "", session_id: str | None = None):
    """代理律师模式 SSE 流式: token/tool/reasoning(CoT)/interrupt/answer/done。"""
    return await _mode_stream("attorney", query, "", session_id)


@app.get("/assistant/ask/stream")
async def assistant_ask_stream(
    case_details: str = "", doc_type: str = "complaint", session_id: str | None = None
):
    """律师助理模式 SSE 流式: CoT + 法条/案例检索过程 + 文书 token 流。"""
    if doc_type not in ("complaint", "defense"):
        raise HTTPException(status_code=422, detail="doc_type 必须为 complaint|defense")
    return await _mode_stream("assistant", case_details, doc_type, session_id)
async def ask_pdf(request: QueryRequest):
    """生成 PDF 报告并返回文件下载."""
    from lawApp_LangGraph.tools.tools import markdown_to_pdf

    sid = ensure_session(request.session_id)
    set_session(sid)
    flow.info("PDF流程开始", summary="用户提问", detail=f"query={request.query[:80]}")

    t0 = time.time()
    graph = get_graph()
    state = await graph.ainvoke({"query": request.query}, config=graph_config(sid))

    answer = state.get("final_answer", "")
    if not answer:
        flow.error("PDF流程失败", detail="未能生成回答内容")
        raise HTTPException(status_code=500, detail="未能生成回答内容")

    safe_name = request.query[:30].strip().replace(" ", "_").replace("/", "_")
    filename = f"legal_answer_{safe_name}.pdf"
    result = await markdown_to_pdf.ainvoke(
        {"markdown_text": answer, "filename": filename}
    )

    pdf_path = result.get("pdf_path") if isinstance(result, dict) else None
    if not pdf_path or not os.path.exists(pdf_path):
        flow.error("PDF流程失败", detail="PDF 生成失败")
        raise HTTPException(status_code=500, detail="PDF 生成失败")

    elapsed = time.time() - t0
    flow.info(
        "PDF流程结束",
        summary="PDF 报告已生成",
        detail=f"file={filename}",
        result=f"总耗时={elapsed:.2f}s",
    )
    return FileResponse(
        path=pdf_path,
        filename=os.path.basename(pdf_path),
        media_type="application/pdf",
    )


@app.post("/feedback")
async def feedback(request: FeedbackRequest):
    """记录用户对回答的评分反馈."""
    from lawApp_LangGraph.db import record_feedback

    try:
        await record_feedback(request.session_id, request.rating, request.comment)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"反馈写入失败: {e}")
    return {"status": "success", "message": "感谢您的反馈"}


@app.get("/tools", response_model=list[ToolInfo])
async def list_tools():
    """返回 Agent 可用的全部工具列表及描述."""
    from lawApp_LangGraph.tools import ALL_TOOLS

    tools = [
        ToolInfo(name=t.name, description=t.description or "") for t in ALL_TOOLS()
    ]
    system.info("工具列表查询", result=f"共 {len(tools)} 个工具可用")
    return tools


@app.get("/home")
async def home():
    from lawApp_LangGraph import runtime

    return {
        "service": "Legal Consultation API",
        "version": "3.1.0",
        "checkpoint_backend": runtime.checkpoint_backend,
        "endpoints": {
            "ask": "POST /ask",
            "ask_stream": "GET /ask/stream?query=xxx&session_id=xxx",
            "ask_resume": "POST /ask/resume (HITL 回传)",
            "ask_pdf": "POST /ask/pdf",
            "feedback": "POST /feedback",
            "tools": "GET /tools",
            "home": "GET /home",
        },
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
