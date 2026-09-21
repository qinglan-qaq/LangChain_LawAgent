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

import asyncio
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
from lawApp_LangGraph.tracing import RunContext, Span, attach_state, flush_run, set_run
from lawApp_LangGraph.FastAPI.logging import (
    flow,
    set_session,
    setup_logging,
    system,
)
from lawApp_LangGraph.FastAPI.model import (
    AssistantAskRequest,
    AssistantStreamRequest,
    AttorneyAskRequest,
    FeedbackRequest,
    QueryRequest,
    QueryResponse,
    ResumeRequest,
    ToolInfo,
)
from lawApp_LangGraph.FastAPI.utils import (
    _NO,
    _YES,
    build_response,
    build_tool_usage,
    ensure_session,
    extract_interrupt,
    get_graph,
    graph_config,
    is_command_word,
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

# 同会话并发流防护(H4): 并发 SSE 互踩 reasoning 总线/checkpoint 写
# session_id → 流锁;SSE 端点进入前抢锁, 已占用 → 409 session_busy
_SESSION_LOCKS: dict[str, asyncio.Lock] = {}


async def _acquire_session_lock(sid: str) -> asyncio.Lock:
    """获取会话流锁;已被其他流占用 → 409 session_busy。

    返回已持有的锁, 由 _run_sse 生成器 finally 释放(断连也释放)。
    locked() 检查与 acquire 之间无 await(同事件循环内无竞态窗口)。
    """
    lock = _SESSION_LOCKS.setdefault(sid, asyncio.Lock())
    if lock.locked():
        raise HTTPException(status_code=409, detail="session_busy")
    await lock.acquire()
    return lock


def _validate_resume_session(session_id: str | None) -> str:
    """resume 路径守卫(H5): 空/纯空白 sid → 400;不建新线程。

    resume 与新提问不同: 空 sid 被 ensure_session 当"新建"处理会开出
    一个注定无 checkpoint 的新线程, 图从头执行, 用户材料丢失。
    """
    sid = (session_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="invalid_session_id")
    return sid


async def _require_pending_interrupt(sid: str) -> dict:
    """resume 路径守卫(H5): 无 pending interrupt → 400 no_pending_interrupt。

    Returns:
        待恢复的 interrupt 载荷(含 type/message/question)。
    """
    graph = get_graph()
    snapshot = await graph.aget_state(graph_config(sid))
    interrupt_req = extract_interrupt(snapshot)
    if interrupt_req is None:
        raise HTTPException(status_code=400, detail="no_pending_interrupt")
    return interrupt_req


async def _normalize_resume_with_degrade(
    interrupt_type: str, answer: str, request_text: str
) -> object:
    """normalize_resume + LLM 失败降级(M4)。

    旧实现裸调 normalize_resume(在端点 try 之外), semantic_confirm 的 LLM
    一失败 → 裸 500 且 interrupt 悬死。HITL 确认链路可用性优先(用户已批准
    的「业务错误显式抛出」例外): 失败时降级为指令词/确认词精确匹配,
    risk/pdf 默认拒绝(abort)、degrade 默认终止(abort)、budget 默认收尾
    (finish), 并记 warning。
    """
    try:
        return await normalize_resume(interrupt_type, answer, request_text)
    except Exception as e:
        system.warning(
            "semantic_confirm 失败,降级短词精确匹配",
            detail=f"type={interrupt_type} | err={str(e)[:150]}",
        )
        ans = (answer or "").strip()
        lowered = ans.lower()
        if interrupt_type in ("risk_confirm", "pdf_confirm"):
            if lowered in _YES:
                return True
            if lowered in _NO:
                return False
            return False  # 默认 abort(拒绝)
        if interrupt_type == "degrade_confirm":
            cmd = is_command_word(ans)
            if cmd == "retry":
                return "retry"
            if cmd == "skip":
                return "skip"
            if cmd == "abort":
                return "abort"
            return "abort"  # 默认 abort(终止)
        if interrupt_type == "budget_confirm":
            if not ans:
                return "finish"
            cmd = is_command_word(ans)
            if cmd in ("finish", "abort"):
                return "finish"
            if cmd == "continue":
                return ans  # 继续补充
            return "finish"  # 默认 finish(收尾)
        # clarify / mid_clarify: 原文透传(不经 LLM, 仅防御)
        return ans


async def _finalize_or_interrupt(session_id: str, state_values) -> QueryResponse:
    """构建响应:若命中 interrupt 则附带请求体,并写审计."""
    graph = get_graph()
    config = graph_config(session_id)
    try:
        snapshot = await graph.aget_state(config)
        interrupt_req = extract_interrupt(snapshot)
    except Exception as e:
        # M15: PG 掉线不打穿已跑完的结果 —— 用已有 state_values 降级返回
        # (不附 interrupt), 观测旁路记日志放行
        flow.warning(
            "读取图快照失败,降级不带 interrupt 返回",
            detail=str(e)[:200],
        )
        interrupt_req = None
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
    sid = ensure_session(request.session_id, "attorney")
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
    sid = ensure_session(request.session_id, "attorney")
    set_session(sid)
    await _safe_upsert_session(sid)
    flow.info(
        "流程开始", summary="代理律师模式提问", detail=f"query={request.query[:80]}"
    )
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
    sid = ensure_session(request.session_id, "assistant")
    set_session(sid)
    await _safe_upsert_session(sid)
    doc_label = "起诉状" if request.doc_type == "complaint" else "答辩状"
    flow.info(
        "流程开始",
        summary=f"律师助理模式起草{doc_label}",
        detail=f"案情={request.case_details[:80]}",
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
    """会话列表(sessions 表)。PG 断连降级为空列表 + ERROR 日志,不再 500(规格故事 22)。"""
    from lawApp_LangGraph.db import get_pool

    try:
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT session_id, meta, last_active_at FROM sessions "
                "ORDER BY last_active_at DESC LIMIT 50"
            )
            rows = await cur.fetchall()
    except Exception as e:
        flow.error("会话列表降级", detail=str(e))
        return []
    return [
        {"session_id": r[0], "meta": r[1], "last_active_at": str(r[2])} for r in rows
    ]


@app.get("/sessions/{sid}")
async def get_session(sid: str):
    """单会话详情: 最新快照 + interrupt 状态(等待回复时返回待回答问题)。"""
    graph = get_graph()
    try:
        snap = await graph.aget_state(graph_config(sid))
    except Exception as e:
        # M15: PG 掉线 → 降级返回会话基本信息(interrupt 等置 None), 不抛 500
        flow.error("会话快照读取失败,降级返回基本信息", detail=str(e)[:200])
        return {
            "session_id": sid,
            "query": "",
            "final_answer": "",
            "final_prompt": None,
            "sources": None,
            "tool_calls": None,
            "reasoning": None,
            "interrupt": None,
            "prompts_record": None,
            "elements": None,
            "tool_usage": None,
            "degraded": True,
        }
    if not snap or not snap.values:
        raise HTTPException(status_code=404, detail=f"会话 {sid} 不存在")
    response = build_response(snap.values, sid)
    return {**response.model_dump(), "interrupt": extract_interrupt(snap)}


@app.post("/ask/resume", response_model=QueryResponse)
async def ask_resume(request: ResumeRequest):
    """HITL 继续: 用户对 interrupt 的回复经 Command(resume=...) 回传,图从暂停点恢复."""
    # H5: 空/纯空白 sid → 400(不建新线程);无 pending interrupt → 400
    sid = _validate_resume_session(request.session_id)
    set_session(sid)
    flow.info("HITL 恢复", summary="用户回传", detail=f"answer={request.answer[:60]}")

    config = graph_config(sid)

    # 先读快照取 interrupt 类型,再类型感知归一(v4: 确认类自由文本走 LLM 语义判断;
    # M4: LLM 失败降级短词精确匹配, 不再裸 500 悬死 interrupt)
    interrupt_req = await _require_pending_interrupt(sid)
    itype = (interrupt_req or {}).get("type", "")
    request_text = (interrupt_req or {}).get("message") or (interrupt_req or {}).get(
        "question"
    ) or ""
    resume_value = await _normalize_resume_with_degrade(itype, request.answer, request_text)

    graph = get_graph()
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


@app.post("/ask/resume/stream")
async def ask_resume_stream(request: ResumeRequest):
    """HITL 继续(SSE 流式): 与 POST /ask/resume 同参, 但 resume 后的全过程
    (planner CoT / 节点状态 / 工具调用 / interrupt / answer / tool_usage)
    实时下发事件 —— 正常咨询的主干(planner→executor→finalize)发生在 resume 阶段,
    纯 REST 版看不到任何过程。"""
    # H5: 空/纯空白 sid → 400(不建新线程);无 pending interrupt → 400
    sid = _validate_resume_session(request.session_id)
    set_session(sid)
    flow.info("HITL 恢复", summary="用户回传(流式)", detail=f"answer={request.answer[:60]}")

    # 先读快照取 interrupt 类型,再类型感知归一(确认类自由文本走 LLM 语义判断;
    # M4: LLM 失败降级短词精确匹配, 不再裸 500 悬死 interrupt)
    interrupt_req = await _require_pending_interrupt(sid)
    itype = (interrupt_req or {}).get("type", "")
    request_text = (
        (interrupt_req or {}).get("message")
        or (interrupt_req or {}).get("question")
        or ""
    )
    resume_value = await _normalize_resume_with_degrade(itype, request.answer, request_text)

    # H4: 同会话并发流防护 —— 抢锁失败 409, 成功后由 _run_sse finally 释放
    lock = await _acquire_session_lock(sid)
    return _run_sse(
        sid, Command(resume=resume_value), mode="attorney", session_lock=lock
    )


@app.get("/ask/stream")
async def ask_stream(query: str = "", session_id: str | None = None):
    """(deprecated — 请改用 /attorney/ask/stream|/assistant/ask/stream, 二期移除)
    SSE 流式问答: updates 驱动进度,messages 驱动 token 打字机,values 收尾."""
    if not query.strip():
        raise HTTPException(status_code=422, detail="query 不能为空")

    sid = ensure_session(session_id, "attorney")
    set_session(sid)
    flow.info("流式流程开始", summary="用户提问", detail=f"query={query[:80]}")
    config = graph_config(sid)
    graph = get_graph()
    # H4: 同会话并发流防护 —— 抢锁失败 409, 生成器 finally 释放(断连也释放)
    session_lock = await _acquire_session_lock(sid)

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
        finally:
            # H4: 会话流锁随生成器退出释放(含客户端断连的 GeneratorExit)
            session_lock.release()

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _validate_stream_text(text: str, limit: int = 4000) -> None:
    """GET 流式端点的 URL 长度防护(用户决策: 直接报错不截断)。"""
    if len(text) > limit:
        raise HTTPException(
            status_code=413, detail=f"文本过长({len(text)} > {limit} 字), 请分批发送"
        )


# SSE keepalive 注释帧间隔(H10): 流未结束期间每 15s 发一行 ": ping",
# 防中间层(nginx/代理)因空闲超时掐断长连接;前端解析器忽略 ":" 开头行
_SSE_PING_SECONDS = 15


def _run_sse(
    sid: str,
    astream_input,
    mode: str = "",
    session_lock: asyncio.Lock | None = None,
) -> StreamingResponse:
    """SSE 流式工厂: astream_input(新提问 dict 或 Command(resume=...)) 驱动图。

    ask 与 resume 两种流共用: 事件协议与 /ask/stream 一致
    (token/progress/tool_call/tool_result/elements/reasoning[CoT/状态/计划]/
    session_id/interrupt/answer/prompts_record/tool_usage/done)。
    全链路 trace: event_stream 顶部建 RunContext, run()/pump() 子任务继承
    contextvars → 节点/工具/llm span 自动归集, finally flush_run 落库(观测旁路)。
    session_lock(H4): 调用端抢到的会话流锁, 生成器 finally 释放(断连也释放)。
    """
    graph = get_graph()
    config = graph_config(sid)
    from lawApp_LangGraph.LangGraph_lawApp import (
        close_reasoning_channel,
        open_reasoning_channel,
    )

    async def event_stream():
        import asyncio
        from datetime import datetime

        # run 上下文先于 task 创建(子任务继承 contextvar); resume 用 Command 输入
        run_type = "live_resume" if isinstance(astream_input, Command) else "live_ask"
        trace_run = set_run(RunContext(
            run_id=f"{sid}:{datetime.now():%H%M%S%f}",
            session_id=sid, run_type=run_type, mode=mode,
            query=(astream_input.get("query") or "")
            if isinstance(astream_input, dict) else "",
        ))

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
            # values 流回填(决策 5): 本 super-step 更新过的节点名,
            # 下一帧 values 到达时对应 node span 补 state 快照
            pending_nodes: list[str] = []
            try:
                async for stream_mode, chunk in graph.astream(
                    astream_input,
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
                            if (
                                node_name in ("planner", "replanner")
                                and "plan" in updates
                            ):
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
                                    await out_q.put(
                                        ("tool_call", step.tool_name or "无")
                                    )
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
                            if (
                                node_name == "element_assess"
                                and "case_elements" in updates
                            ):
                                ce = updates.get("case_elements")
                                elems = getattr(ce, "elements", None) or []
                                await out_q.put(
                                    (
                                        "elements",
                                        [
                                            {
                                                "key": e.key,
                                                "label": e.label,
                                                "status": e.status,
                                            }
                                            for e in elems
                                        ],
                                    )
                                )
                        pending_nodes.extend(
                            str(n) for n in (chunk or {}) if isinstance(n, str)
                        )
                    elif stream_mode == "values":
                        final_state = chunk or final_state
                        # values 流回填: 本 super-step 后的完整 state → 对应 node span
                        attach_state(pending_nodes, chunk or {})
                        pending_nodes = []

                snapshot = await graph.aget_state(config)
                interrupt_req = extract_interrupt(snapshot)
                if interrupt_req:
                    # interrupt 分支也必须下发 session_id, 否则前端 resume 时
                    # 带空 id → 生成新线程 → 图从头执行(query 为空被误判闲聊)
                    await out_q.put(("session_id", sid))
                    await out_q.put(("interrupt", interrupt_req))
                    await _safe_audit(sid, "hitl_interrupt", interrupt_req)
                    trace_run.status = "interrupted"
                    trace_run.add(Span(span_type="hitl",
                                       name=interrupt_req.get("type", "hitl"),
                                       input=interrupt_req, started_at=time.time()))
                else:
                    answer = (final_state.get("final_answer") or "").strip()
                    trace_run.final_answer = answer
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
                    # 工具使用 JSON 记录: {tool_name: [结果摘要, ...]}(用户决策 v4)
                    tool_usage = build_tool_usage(final_state)
                    if tool_usage:
                        await out_q.put(("tool_usage", tool_usage))
                    await _safe_audit(
                        sid,
                        "citations",
                        {"tool_calls": final_state.get("tool_calls", [])},
                    )
            except asyncio.CancelledError:
                # M5: 客户端断开时任务被取消 → trace 记 cancelled 而非留 "ok"
                trace_run.status = "cancelled"
                raise
            except Exception as e:
                flow.error("流式流程异常", detail=str(e))
                trace_run.status = "error"
                await out_q.put(("error", str(e)))
            finally:
                # trace 落库先于 done 终止帧(观测旁路: 失败内部记 ERROR 放行, 决策 8)
                await flush_run(trace_run)
                # 唤醒 pump 排空残余 CoT 增量, done 由 pump 统一发出
                await reasoning_q.put(None)

        async def ping():
            """keepalive(H10): 流期间周期性向输出队列塞注释帧标记。"""
            while True:
                await asyncio.sleep(_SSE_PING_SECONDS)
                await out_q.put((":ping", ""))

        tasks = [
            asyncio.create_task(run()),
            asyncio.create_task(pump()),
            asyncio.create_task(ping()),
        ]
        saw_done = False  # M5: 是否收到终止帧(正常收场判定)
        try:
            while True:
                item = await out_q.get()
                if item is None:
                    break
                if item[0] == ":ping":
                    # SSE 注释帧: 纯 keepalive, 前端解析器忽略 ":" 开头行
                    yield ": ping\n\n"
                    continue
                if item[0] == "done":
                    saw_done = True
                yield sse_event(item[0], item[1])
        finally:
            for t in tasks:
                t.cancel()
            close_reasoning_channel(sid, reasoning_q)
            # H4: 会话流锁随生成器退出释放(含客户端断连的 GeneratorExit)
            if session_lock is not None:
                session_lock.release()
            # M5: 客户端断开(生成器被 close → GeneratorExit/子任务取消)时
            # trace 记 cancelled 而非留 "ok";done 帧已收到或已写终态
            # (error/interrupted)时不覆盖
            if not saw_done and trace_run.status == "ok":
                trace_run.status = "cancelled"
                try:
                    await flush_run(trace_run)
                except Exception:
                    pass  # 观测旁路, 落库失败放行

    return StreamingResponse(event_stream(), media_type="text/event-stream")


async def _mode_stream(
    mode: str, query: str, doc_type: str, session_id: str | None
) -> StreamingResponse:
    """双模式 SSE 流式问答: 旧 /ask/stream 全事件协议 + reasoning CoT 流。

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
    sid = ensure_session(session_id, mode)
    set_session(sid)
    await _safe_upsert_session(sid)

    inputs = {"query": query, "mode": mode}
    if mode == "assistant":
        inputs["doc_type"] = doc_type

    # H4: 同会话并发流防护 —— 抢锁失败 409, 成功后由 _run_sse finally 释放
    lock = await _acquire_session_lock(sid)
    return _run_sse(sid, inputs, mode=mode, session_lock=lock)


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


@app.post("/assistant/ask/stream")
async def assistant_ask_stream_post(request: AssistantStreamRequest):
    """律师助理模式 SSE 流式(POST 版, M11): 长案情(4000 CJK)经 URL 传输会
    超浏览器/代理 URL 长度限制 → 改 body 传参; 事件协议与 GET 完全一致
    (复用 _mode_stream, 含校验/409 会话锁/ensure_session), GET 保留兼容。"""
    return await _mode_stream(
        "assistant", request.case_details, request.doc_type, request.session_id
    )


@app.post("/ask/pdf", response_model=QueryResponse)
async def ask_pdf(request: QueryRequest):
    """生成 PDF 报告并返回文件下载."""
    from lawApp_LangGraph.tools.tools import markdown_to_pdf

    sid = ensure_session(request.session_id, "attorney")
    set_session(sid)
    flow.info("PDF流程开始", summary="用户提问", detail=f"query={request.query[:80]}")

    t0 = time.time()
    graph = get_graph()
    try:
        state = await graph.ainvoke(
            {"query": request.query}, config=graph_config(sid)
        )
    except Exception as e:
        flow.error("PDF流程失败", summary="Graph 执行失败", detail=str(e))
        raise HTTPException(status_code=500, detail=f"Graph 执行失败: {e}")

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
