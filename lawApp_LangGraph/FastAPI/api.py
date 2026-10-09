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
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
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
    MonitorEval,
    MonitorOverview,
    MonitorRunDetail,
    MonitorRunItem,
    MonitorSpan,
    MonitorStage,
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

# 标准库 logger(观测旁路告警, propagate=True 便于测试/采集捕获;
# system/flow 包装 logger propagate=False, 不适合旁路告警)
logger = logging.getLogger("lawApp.api")


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

    # L19: DEEPSEEK_API_KEY 缺失在启动期显式爆(fail loud), 不再等到首次
    # LLM 调用才炸;只在此处(startup 路径)抛, 模块 import 不触发
    if not (settings.deepseek_api_key or "").strip():
        system.error(
            "启动校验失败", detail="DEEPSEEK_API_KEY 未配置, LLM 全链路不可用"
        )
        raise RuntimeError(
            "DEEPSEEK_API_KEY 未配置: LLM 全链路(规划/分析/语义确认)不可用, "
            "请在 lawApp_LangGraph/.env 配置后重启服务"
        )
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
        if interrupt_type in ("risk_confirm", "pdf_confirm", "docx_confirm"):
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
        # L1: 审计旁路失败零日志曾完全静默 —— 记 warning 带堆栈(对齐
        # _safe_upsert_session 模式), 不影响主流程
        logger.warning("审计写入失败(旁路)", exc_info=True)


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
        state = await graph.ainvoke(
            {"query": request.query,
             "messages": [{"role": "user", "content": request.query}]},
            config=config,
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
            {"query": request.query, "mode": "attorney",
             "messages": [{"role": "user", "content": request.query}]},
            config=graph_config(sid),
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
                "messages": [{"role": "user", "content": request.case_details}],
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
            "clarify_history": None,
            "tool_usage": None,
            "degraded": True,
        }
    if not snap or not snap.values:
        raise HTTPException(status_code=404, detail=f"会话 {sid} 不存在")
    response = build_response(snap.values, sid)
    return {**response.model_dump(), "interrupt": extract_interrupt(snap)}


@app.get("/sessions/{sid}/dialogue")
async def get_session_dialogue(sid: str):
    """会话级对话历史(方案c: JSON 事件流聚合)。

    契约(前端并行开发依赖, 不得偏离):
        {"session_id", "rounds": [...], "confirms": [...], "final": {...}|None}
    空态(rounds/confirms 空列表、final=None)与 PG 掉线均返回 200 空结构
    (对齐 GET /sessions 降级先例, 前端好处理); 非法 sid → 400。
    """
    from lawApp_LangGraph import dialogue_log
    from lawApp_LangGraph.FastAPI.utils import (
        _SESSION_ID_NEW_RE,
        _SESSION_ID_OLD_RE,
    )

    sid = (sid or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="invalid_session_id")
    # sid 校验: 复用 utils 的格式规则(带 AT-/AS- 前缀但格式非法 → 400);
    # 会话历史不区分模式, 不做前缀与端点模式一致性校验(AS- 会话同样可查)
    if sid.startswith(("AT-", "AS-")) and not (
        _SESSION_ID_NEW_RE.match(sid) or _SESSION_ID_OLD_RE.match(sid)
    ):
        raise HTTPException(status_code=400, detail="invalid_session_id")

    try:
        return await dialogue_log.aggregate_dialogue(sid)
    except Exception as e:  # pragma: no cover — aggregate 内部已吞, 此处双保险
        flow.error("对话历史读取降级", detail=str(e)[:200])
        return {
            "session_id": sid,
            "rounds": [],
            "confirms": [],
            "final": None,
            "docx": None,
        }


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
                            # L2: 以 doing 步骤定位刚发起的工具调用(见助手注释)
                            for tn in _executor_tool_names(updates):
                                yield sse_event("tool_call", tn)
                        if node_name == "merge":
                            # docx 渲染完成(docx_path 经 merge 回填 state)→
                            # 前端 toast/下载入口(Task 5 契约帧)
                            if updates.get("docx_path"):
                                yield sse_event(
                                    "docx_done",
                                    {"path": updates["docx_path"]},
                                )
                            # L2: 工具结果帧 — 定位刚完成步骤(见助手注释)
                            summary = _merge_tool_summary(updates)
                            if summary:
                                yield sse_event("tool_result", summary)
                        if node_name == "element_assess" and "case_elements" in updates:
                            ce = updates.get("case_elements")
                            elems = getattr(ce, "elements", None) or []
                            # L4: 要素面板显隐由后端显式标记(chitchat 不展示)
                            yield sse_event(
                                "elements",
                                {
                                    "elements": [
                                        {"key": e.key, "label": e.label, "status": e.status}
                                        for e in elems
                                    ],
                                    "is_case_query": updates.get("question_category")
                                    != "chitchat",
                                },
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
            yield sse_event("done", "")
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


# L2: SSE tool_call/tool_result 定位助手 —— executor 成功路径不回写
# current_step_index(由 merge 推进), 旧实现以该键定位导致真实工具调用
# 从不发出 tool_call 帧(仅失败/无工具分支可达)。现改为:
# - tool_call 以 plan 内 doing 步骤定位(executor 刚发起的调用)
# - tool_result 以 merge 回写的 current_step_index-1 定位刚完成步骤,
#   覆盖全部工具(不止数据检索键), 检索键计数拼进摘要


def _executor_tool_names(updates: dict) -> list:
    """executor 节点 updates → 刚发起调用的工具名列表(doing 步骤)。"""
    names = []
    for s in updates.get("plan") or []:
        if getattr(s, "status", "") == "doing" and s.tool_name:
            names.append(s.tool_name)
    return names


def _merge_tool_summary(updates: dict) -> Optional[str]:
    """merge 节点 updates → 刚完成步骤的工具结果摘要; 无工具/越界返回 None。"""
    ci = updates.get("current_step_index")
    plan = updates.get("plan") or []
    if ci is None or not (0 < ci <= len(plan)):
        return None
    st = plan[ci - 1]
    if not getattr(st, "tool_name", None):
        return None
    bits = []
    for k in ("rag_documents", "web_search_results", "law_results", "evaluation"):
        if k in updates and updates[k]:
            bits.append(
                f"{k} {len(updates[k]) if isinstance(updates[k], list) else 1}"
            )
    if bits:
        return " ".join(bits)
    return "执行完成" if getattr(st, "status", "") == "done" else "执行失败"


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
                                # L2: 以 doing 步骤定位刚发起的工具调用(见助手注释)
                                for tn in _executor_tool_names(updates):
                                    await out_q.put(("tool_call", tn))
                            if node_name == "merge":
                                # docx 渲染完成(docx_path 经 merge 回填 state)→
                                # 前端 toast/下载入口(Task 5 契约帧;
                                # sse_event 对 dict data 直接 JSON 序列化)
                                if updates.get("docx_path"):
                                    await out_q.put(
                                        (
                                            "docx_done",
                                            {"path": updates["docx_path"]},
                                        )
                                    )
                                # L2: 工具结果帧 — 定位刚完成步骤(见助手注释)
                                summary = _merge_tool_summary(updates)
                                if summary:
                                    await out_q.put(("tool_result", summary))
                            if (
                                node_name == "element_assess"
                                and "case_elements" in updates
                            ):
                                ce = updates.get("case_elements")
                                elems = getattr(ce, "elements", None) or []
                                await out_q.put(
                                    (
                                        "elements",
                                        {
                                            "elements": [
                                                {
                                                    "key": e.key,
                                                    "label": e.label,
                                                    "status": e.status,
                                                }
                                                for e in elems
                                            ],
                                            # L4: 要素面板显隐由后端显式标记
                                            "is_case_query": updates.get(
                                                "question_category"
                                            )
                                            != "chitchat",
                                        },
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
    # L13: 对话历史写侧 — 每轮提问以 user 消息入 state.messages
    # (add_messages 跨轮持久, finalize 追加 assistant 条完成闭环)
    inputs["messages"] = [{"role": "user", "content": query}]

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
            {"query": request.query,
             "messages": [{"role": "user", "content": request.query}]},
            config=graph_config(sid),
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


async def _session_doc_path(session_id: str) -> str:
    """会话最新 docx 路径(Task 6: 自 session_docx_latest 抽出的共用实现)。

    事件查询 + DOCX_OUTPUT_DIR 白名单; 404 三态: 本会话无 docx_generated 事件 /
    事件指向的文件已不存在 / 事件里的路径越出 DOCX_OUTPUT_DIR
    (事件被篡改或迁移残留, spec §7 双防线第二道)。
    """
    from lawApp_LangGraph.db import get_pool
    from lawApp_LangGraph.FastAPI.utils import (
        _SESSION_ID_NEW_RE,
        _SESSION_ID_OLD_RE,
    )

    # sid 校验复用 dialogue 端点同款规则(带 AT-/AS- 前缀但格式非法 → 400;
    # 无前缀历史 sid 放行; 不做模式一致性校验)
    sid = (session_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="invalid_session_id")
    if sid.startswith(("AT-", "AS-")) and not (
        _SESSION_ID_NEW_RE.match(sid) or _SESSION_ID_OLD_RE.match(sid)
    ):
        raise HTTPException(status_code=400, detail="invalid_session_id")

    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT payload FROM session_dialogue_events "
            "WHERE session_id = %s AND event_type = 'docx_generated' "
            "ORDER BY seq DESC LIMIT 1",
            (sid,),
        )
        row = await cur.fetchone()
    if not row or not (row[0] or {}).get("docx_path"):
        raise HTTPException(status_code=404, detail="本会话尚未生成 Word 文书")
    path = row[0]["docx_path"]
    # 路径白名单: 事件路径必须落在 DOCX_OUTPUT_DIR 之内(防穿越)
    base = os.path.abspath(os.getenv("DOCX_OUTPUT_DIR", "./docx_outputs"))
    if not os.path.abspath(path).startswith(base + os.sep):
        raise HTTPException(status_code=404, detail="文书文件不存在或路径非法")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="文书文件不存在或路径非法")
    return path


def _convert_docx_pdf(docx_path: str, pdf_path: str) -> None:
    """同步 docx→pdf(Word COM); 模块级便于测试 monkeypatch。"""
    from docx2pdf import convert
    convert(docx_path, pdf_path)


def _remove_partial_pdf(pdf_path: str) -> None:
    """转换中断的半成品 pdf 若残留在盘上, 会以新 mtime 命中缓存被当有效文件返回, 必须删除;
    清理尽力而为且绝不抛错 — 不得吞掉/掩盖原始转换异常。"""
    try:
        os.remove(pdf_path)
    except OSError:
        pass


async def _docx_to_pdf(docx_path: str) -> str:
    """docx → pdf(Word COM, 30s 超时); 输出 PDF_OUTPUT_DIR 同名 .pdf, mtime 缓存。"""
    pdf_dir = os.path.abspath(os.getenv("PDF_OUTPUT_DIR", "./pdf_outputs"))
    os.makedirs(pdf_dir, exist_ok=True)
    pdf_path = os.path.join(pdf_dir, os.path.splitext(os.path.basename(docx_path))[0] + ".pdf")
    if os.path.exists(pdf_path) and os.path.getmtime(pdf_path) >= os.path.getmtime(docx_path):
        return pdf_path
    try:
        await asyncio.wait_for(
            asyncio.to_thread(_convert_docx_pdf, docx_path, pdf_path), timeout=30
        )
    except TimeoutError:
        _remove_partial_pdf(pdf_path)
        raise HTTPException(status_code=502, detail="PDF 转换超时(30s)")
    except Exception as e:
        _remove_partial_pdf(pdf_path)
        raise HTTPException(
            status_code=502, detail=f"PDF 转换失败(需本机安装 Microsoft Word): {e}"
        )
    return pdf_path


@app.get("/sessions/{session_id}/doc/{format}")
async def session_doc(session_id: str, format: str):
    """双格式文书下载: docx=原文件, pdf=Word COM 转换(mtime 缓存)。

    非法 format → 400(不触文件查找); Task 7 前端下载按钮 href 依赖本路由。
    """
    if format not in ("docx", "pdf"):
        raise HTTPException(status_code=400, detail="invalid_format")
    path = await _session_doc_path(session_id)
    if format == "docx":
        return FileResponse(
            path=path,
            filename=os.path.basename(path),
            media_type=(
                "application/vnd.openxmlformats-officedocument"
                ".wordprocessingml.document"
            ),
        )
    pdf = await _docx_to_pdf(path)
    return FileResponse(
        path=pdf, filename=os.path.basename(pdf), media_type="application/pdf"
    )


@app.get("/sessions/{session_id}/docx/latest")
async def session_docx_latest(session_id: str):
    """旧路径兼容别名 → docx 分支(Task 7 前端切换前契约不变)。"""
    return await session_doc(session_id, "docx")


@app.post("/feedback")
async def feedback(request: FeedbackRequest):
    """记录用户对回答的评分反馈.

    L1: 反馈是旁路观测, 与其他写路径统一契约 —— 写入失败不 500
    (其他审计/会话登记失败均静默放行, 唯独这里打穿用户体验),
    记 warning 返回 200 + degraded 标记."""
    from lawApp_LangGraph.db import record_feedback

    try:
        await record_feedback(request.session_id, request.rating, request.comment)
    except Exception:
        logger.warning("反馈写入失败(旁路)", exc_info=True)
        return {
            "status": "ok",
            "degraded": True,
            "message": "反馈记录暂不可用,感谢您的反馈",
        }
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


#  监控页端点(D-spec §五): PG 断连降级空态 + flow.error, 不 500
@app.get("/monitor/overview", response_model=MonitorOverview)
async def monitor_overview():
    from lawApp_LangGraph.db import get_pool

    by_status: dict = {}
    running = limit_hit = 0
    fails: list = []
    dist: dict = {}
    try:
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT status, COUNT(*) FROM trace_runs "
                "WHERE started_at > NOW() - INTERVAL '24 hours' "
                "GROUP BY status"
            )
            by_status = {r[0]: r[1] for r in await cur.fetchall()}
            cur = await conn.execute(
                "SELECT COUNT(*) FROM stage_chain WHERE ended_at IS NULL"
            )
            running = (await cur.fetchone())[0]
            cur = await conn.execute(
                "SELECT COUNT(*) FROM trace_runs "
                "WHERE metrics->>'limit_hit' = 'true'"
            )
            limit_hit = (await cur.fetchone())[0]
            cur = await conn.execute(
                "SELECT node_name, COUNT(*) FROM stage_chain "
                "WHERE status = 'error' "
                "AND started_at > NOW() - INTERVAL '24 hours' "
                "GROUP BY node_name ORDER BY 2 DESC LIMIT 5"
            )
            fails = [{"node_name": r[0], "errors": r[1]}
                     for r in await cur.fetchall()]
            # 分数分布: [0,34) / [34,67) / [67,100] 三桶
            cur = await conn.execute(
                "SELECT width_bucket("
                "  (metrics->>'composite_score')::numeric, 0, 101, 3) AS b,"
                "  COUNT(*) FROM trace_runs "
                "WHERE metrics ? 'composite_score' GROUP BY b"
            )
            dist = {f"bucket_{r[0]}": r[1] for r in await cur.fetchall()}
    except Exception as e:
        flow.error("monitor overview 降级", detail=str(e))
        return MonitorOverview()
    return MonitorOverview(runs_by_status=by_status, running_stages=running,
                           limit_hit_runs=limit_hit, node_fail_top=fails,
                           score_distribution=dist)


@app.get("/monitor/runs", response_model=list[MonitorRunItem])
async def monitor_runs(limit: int = 50, offset: int = 0, status: Optional[str] = None,
                       session_id: Optional[str] = None,
                       response: Response = None):
    from lawApp_LangGraph.db import get_pool

    conds, params = ["1=1"], []
    if status:
        conds.append("r.status = %s")
        params.append(status)
    if session_id:
        conds.append("r.session_id = %s")
        params.append(session_id)
    where = " AND ".join(conds)
    sql = (
        "SELECT r.run_id, r.session_id, r.run_type, r.mode, r.status, "
        "r.started_at, r.ended_at, r.metrics, "
        "(SELECT COUNT(*) FROM stage_chain s WHERE s.run_id = r.run_id), "
        "(SELECT COUNT(*) FROM stage_chain s WHERE s.run_id = r.run_id "
        " AND s.status = 'ok'), "
        "(SELECT COUNT(*) FROM stage_chain s WHERE s.run_id = r.run_id "
        " AND s.ended_at IS NULL) "
        f"FROM trace_runs r WHERE {where} "
        "ORDER BY r.started_at DESC LIMIT %s OFFSET %s"
    )
    page_params = params + [min(limit, 500), max(offset, 0)]
    try:
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(sql, tuple(page_params))
            rows = await cur.fetchall()
            # 分页元信息走响应头, 响应体保持纯列表(契约不变)
            cur = await conn.execute(
                f"SELECT COUNT(*) FROM trace_runs r WHERE {where}", tuple(params))
            total = (await cur.fetchone())[0]
    except Exception as e:
        flow.error("monitor runs 降级", detail=str(e))
        return []
    if response is not None:
        response.headers["X-Total-Count"] = str(total)
    return [
        MonitorRunItem(
            run_id=r[0], session_id=r[1], run_type=r[2], mode=r[3],
            status=r[4], started_at=str(r[5]) if r[5] else None,
            ended_at=str(r[6]) if r[6] else None,
            stage_total=r[8] or 0, stage_ok=r[9] or 0,
            stage_running=r[10] or 0, metrics=r[7] or {},
        )
        for r in rows
    ]


@app.get("/monitor/runs/{run_id}/stages", response_model=MonitorRunDetail)
async def monitor_run_detail(run_id: str):
    from lawApp_LangGraph.db import get_pool

    try:
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT run_id, session_id, run_type, mode, status, "
                "started_at, ended_at, metrics FROM trace_runs "
                "WHERE run_id = %s", (run_id,))
            r = await cur.fetchone()
            if not r:
                raise HTTPException(status_code=404,
                                    detail=f"run {run_id} 不存在")
            cur = await conn.execute(
                "SELECT node_name, seq, status, started_at, ended_at, "
                "latency_ms, detail FROM stage_chain WHERE run_id = %s "
                "ORDER BY started_at, seq", (run_id,))
            stages = await cur.fetchall()
            cur = await conn.execute(
                "SELECT span_type, name, status, input, output, state, "
                "latency_ms, token_usage, started_at FROM trace_spans "
                "WHERE run_id = %s ORDER BY started_at", (run_id,))
            spans = await cur.fetchall()
    except HTTPException:
        raise
    except Exception as e:
        flow.error("monitor detail 降级", detail=str(e))
        raise HTTPException(status_code=503, detail="监控数据暂不可用")
    return MonitorRunDetail(
        run_id=r[0], session_id=r[1], run_type=r[2], mode=r[3],
        status=r[4], started_at=str(r[5]) if r[5] else None,
        ended_at=str(r[6]) if r[6] else None, metrics=r[7] or {},
        stages=[
            MonitorStage(node_name=s[0], seq=s[1], status=s[2],
                         started_at=str(s[3]),
                         ended_at=str(s[4]) if s[4] else None,
                         latency_ms=s[5], detail=s[6] or {})
            for s in stages
        ],
        spans=[
            MonitorSpan(span_type=s[0], name=s[1], status=s[2], input=s[3],
                        output=s[4], state=s[5], latency_ms=s[6],
                        token_usage=s[7],
                        started_at=str(s[8]) if s[8] else None)
            for s in spans
        ],
    )


@app.get("/monitor/evals", response_model=list[MonitorEval])
async def monitor_evals(limit: int = 20):
    from lawApp_LangGraph.db import get_pool

    try:
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id, dataset, label, metrics, created_at "
                "FROM eval_runs ORDER BY created_at DESC LIMIT %s",
                (min(limit, 100),))
            rows = await cur.fetchall()
    except Exception as e:
        flow.error("monitor evals 降级", detail=str(e))
        return []
    return [
        MonitorEval(id=r[0], dataset=r[1], label=r[2], metrics=r[3] or {},
                    created_at=str(r[4]) if r[4] else None)
        for r in rows
    ]


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
