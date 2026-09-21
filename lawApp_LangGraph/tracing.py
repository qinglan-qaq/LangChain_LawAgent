"""
观测插桩 — @traced 装饰器 + run 上下文 + span 收集落库。

规格: docs/superpowers/specs/2026-09-20-eval-monitoring-spec.md(决策 3-6,9)
- @traced(span_type) 包 图节点/工具函数: 记录 函数名/时延/前后成果/执行结果;
异常原样透传(不吞, 先记 error span)
- LLM 层经 InstrumentedChatOpenAI 拦截 _agenerate/_astream(单点, 不逐函数装饰)
- run 上下文 = contextvars;未 set 时 span 进游离缓冲(仅内存, 不落库不报错)
- state 全量快照不在此模块 —— 由 api._run_sse 的 values 流经 attach_state 回填
- flush_run 落库失败记 ERROR 放行(观测旁路, 决策 8)
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger("lawApp.trace")

_current_run: contextvars.ContextVar = contextvars.ContextVar(
    "trace_current_run", default=None
)
_ORPHAN_SPANS: list["Span"] = []
_ORPHAN_CAP = 200


@dataclass
class Span:
    span_type: str  # node | tool | llm | hitl
    name: str
    status: str = "ok"  # ok | error | interrupted
    input: Any = None
    output: Any = None
    state: Any = None  # values 流回填
    latency_ms: int = 0
    token_usage: Optional[dict] = None
    started_at: Optional[float] = None  # 墙钟 epoch, 插桩时写入


@dataclass
class RunContext:
    run_id: str
    session_id: str
    run_type: str  # live_ask | live_resume | eval
    mode: str = ""
    query: str = ""
    final_answer: str = ""
    status: str = "ok"
    spans: list[Span] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)

    def add(self, span: Span) -> None:
        self.spans.append(span)

    def metrics(self) -> dict:
        return {
            "node_count": sum(1 for s in self.spans if s.span_type == "node"),
            "tool_count": sum(1 for s in self.spans if s.span_type == "tool"),
            "llm_count": sum(1 for s in self.spans if s.span_type == "llm"),
            "total_latency_ms": sum(s.latency_ms for s in self.spans),
            "token_prompt": sum(
                (s.token_usage or {}).get("prompt") or 0 for s in self.spans
            ),
            "token_completion": sum(
                (s.token_usage or {}).get("completion") or 0 for s in self.spans
            ),
            "clarify_rounds": sum(
                1
                for s in self.spans
                if s.span_type == "node" and s.name in ("ask_element", "mid_clarify")
            ),
        }


def set_run(run: RunContext) -> RunContext:
    _current_run.set(run)
    return run


def current_run() -> Optional[RunContext]:
    return _current_run.get()


def _emit(span: Span) -> None:
    run = _current_run.get()
    if run is not None:
        run.add(span)
        return
    _ORPHAN_SPANS.append(span)
    if len(_ORPHAN_SPANS) > _ORPHAN_CAP:
        del _ORPHAN_SPANS[: len(_ORPHAN_SPANS) - _ORPHAN_CAP]


def _pack(args: tuple, kwargs: dict) -> Any:
    """前后成果-入参打包: 单位置参原样, 其余 args/kwargs 全记(不截断)。"""
    if not kwargs and len(args) == 1:
        return args[0]
    return {"args": list(args), "kwargs": dict(kwargs)}


def _is_interrupt(e: Exception) -> bool:
    try:
        from langgraph.errors import GraphInterrupt

        return isinstance(e, GraphInterrupt)
    except Exception:
        return False


def traced(span_type: str, name: Optional[str] = None) -> Callable:
    """装饰器工厂(规格决策 3): 包住需检测的图节点/工具函数。

    异常原样透传(先记 error/interrupted span);async 与 sync 函数都支持。
    """

    def deco(fn: Callable) -> Callable:
        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def awrapper(*args, **kwargs):
                t0 = time.perf_counter()
                span = Span(
                    span_type=span_type,
                    name=name or fn.__name__,
                    input=_pack(args, kwargs),
                    started_at=time.time(),
                )
                try:
                    out = await fn(*args, **kwargs)
                except Exception as e:
                    span.status = "interrupted" if _is_interrupt(e) else "error"
                    span.output = {"exception": repr(e)}
                    span.latency_ms = int((time.perf_counter() - t0) * 1000)
                    _emit(span)
                    raise
                span.output = out
                span.latency_ms = int((time.perf_counter() - t0) * 1000)
                _emit(span)
                return out

            return awrapper

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            span = Span(
                span_type=span_type,
                name=name or fn.__name__,
                input=_pack(args, kwargs),
                started_at=time.time(),
            )
            try:
                out = fn(*args, **kwargs)
            except Exception as e:
                span.status = "interrupted" if _is_interrupt(e) else "error"
                span.output = {"exception": repr(e)}
                span.latency_ms = int((time.perf_counter() - t0) * 1000)
                _emit(span)
                raise
            span.output = out
            span.latency_ms = int((time.perf_counter() - t0) * 1000)
            _emit(span)
            return out

        return wrapper

    return deco


def attach_state(node_names: list[str], values: dict) -> None:
    """values 流回填(决策 5): 节点执行后的完整 state 写进对应 node span。

    已回填的 span 不覆盖(节点二次执行时新 span 各自拿新快照)。
    """
    run = _current_run.get()
    if run is None:
        return
    for span in run.spans:
        if span.span_type == "node" and span.name in node_names and span.state is None:
            span.state = values


def _msg_text(m: Any) -> Any:
    content = getattr(m, "content", m)
    role = getattr(m, "type", None)
    return {"role": role, "content": content} if role is not None else content


def _gen_message(result: Any) -> Any:
    """从 ChatResult 取 AIMessage。

    generations 形状随调用路径浮动(ChatResult 扁平 list / 批处理嵌套
    list-of-lists)→ 两种都兼容, 取不出时返回 None(观测旁路不报错)。
    """
    gens = getattr(result, "generations", None) or []
    first = gens[0] if gens else None
    if isinstance(first, (list, tuple)):
        first = first[0] if first else None
    return getattr(first, "message", None)


def _usage_from(msg: Any) -> Optional[dict]:
    """token 用量三处兜底: usage_metadata / response_metadata.token_usage /
    generation_info.token_usage(DeepSeek 流式聚合块的放置位置随版本浮动)。"""
    if msg is None:
        return None
    u = getattr(msg, "usage_metadata", None)
    if isinstance(u, dict) and u.get("input_tokens") is not None:
        return {"prompt": u.get("input_tokens"), "completion": u.get("output_tokens")}
    for holder in (
        getattr(msg, "response_metadata", None),
        getattr(msg, "generation_info", None),
    ):
        if isinstance(holder, dict):
            tu = holder.get("token_usage")
            if isinstance(tu, dict):
                return {
                    "prompt": tu.get("prompt_tokens"),
                    "completion": tu.get("completion_tokens"),
                }
    return None


def _emit_llm_span(
    model: str,
    messages: Any,
    output_msg: Any,
    t0: float,
    status: str,
    content: Any = None,
) -> None:
    span = Span(
        span_type="llm",
        name=f"llm:{model}",
        input=[_msg_text(m) for m in (messages or [])],
        latency_ms=int((time.perf_counter() - t0) * 1000),
        started_at=time.time(),
        status=status,
    )
    if output_msg is not None or content is not None:
        # output_msg 可能是 AIMessage / AIMessageChunk / ChatGenerationChunk(包装)
        msg = getattr(output_msg, "message", output_msg)
        if content is None and msg is not None:
            content = getattr(msg, "content", None)
        span.output = content if content else None
        span.token_usage = _usage_from(msg)
    _emit(span)


async def flush_run(run: RunContext) -> None:
    """run + spans 统一落库;失败记 ERROR 放行(观测旁路, 决策 8)。"""
    try:
        from lawApp_LangGraph import db

        await db.insert_trace_run(
            run_id=run.run_id,
            session_id=run.session_id,
            run_type=run.run_type,
            mode=run.mode,
            status=run.status,
            query=run.query,
            final_answer=run.final_answer,
            metrics=run.metrics(),
            started_at=run.started_at,
            ended_at=time.time(),
        )
        rows = [
            {
                "run_id": run.run_id,
                "span_type": s.span_type,
                "name": s.name,
                "status": s.status,
                "input": s.input,
                "output": s.output,
                "state": s.state,
                "latency_ms": s.latency_ms,
                "token_usage": s.token_usage,
                "started_at": s.started_at,
            }
            for s in run.spans
        ]
        if rows:
            await db.insert_trace_spans(rows)
    except Exception:
        logger.error("trace 落库失败(观测旁路, 不阻塞业务)", exc_info=True)


def _instrument_llm_cls():
    """llm 观测包装类(决策 6): 工厂返回处单点替换, 覆盖 ainvoke/astream 全部调用。"""
    from langchain_openai import ChatOpenAI

    class InstrumentedChatOpenAI(ChatOpenAI):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            t0 = time.perf_counter()
            try:
                result = await super()._agenerate(
                    messages, stop=stop, run_manager=run_manager, **kwargs
                )
            except Exception:
                _emit_llm_span(self.model_name, messages, None, t0, "error")
                raise
            try:
                _emit_llm_span(
                    self.model_name, messages, _gen_message(result), t0, "ok"
                )
            except Exception:
                # 观测旁路: span 记录失败绝不影响 LLM 结果返回
                logger.error("llm span 记录失败(观测旁路)", exc_info=True)
            return result

        async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
            t0 = time.perf_counter()
            last = None
            parts: list[str] = []
            try:
                async for chunk in super()._astream(
                    messages, stop=stop, run_manager=run_manager, **kwargs
                ):
                    last = chunk
                    # chunk 可能是 AIMessageChunk 或 ChatGenerationChunk(包装)
                    m = getattr(chunk, "message", chunk)
                    c = getattr(m, "content", None)
                    if isinstance(c, str) and c:
                        parts.append(c)
                    yield chunk
            except Exception:
                _emit_llm_span(self.model_name, messages, last, t0, "error")
                raise
            try:
                # 流式全文聚合(末块 content 常为空, usage 在末块 metadata)
                _emit_llm_span(
                    self.model_name,
                    messages,
                    last,
                    t0,
                    "ok",
                    content="".join(parts) or None,
                )
            except Exception:
                logger.error("llm span 记录失败(观测旁路)", exc_info=True)

    return InstrumentedChatOpenAI


_INSTRUMENTED = None


def get_instrumented_llm_cls():
    """延迟构建(避免模块导入期强依赖 langchain_openai;每次返回同一类)。"""
    global _INSTRUMENTED
    if _INSTRUMENTED is None:
        _INSTRUMENTED = _instrument_llm_cls()
    return _INSTRUMENTED
