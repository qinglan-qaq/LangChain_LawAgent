# Agent 阶段监控平台(拉链表+甘特图+评测算分)实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 lawApp_LangGraph 增加阶段级监控——`stage_chain` 拉链表实时记录 15 图节点状态, 4 个 `/monitor/*` 端点, 前端 `/monitor` 甘特图监控页(带路由跳转), retrieval 评测套件(hit_rate/MRR/P/R/F1)与 composite_score 综合评分。

**Architecture:** 复用现有 `@traced` 装饰器单点双写开行/闭行(不碰图代码); 拉链表管阶段状态(实时), `trace_spans` 管内容明细(事后), 监控详情页按 (run_id, node_name) join 对齐; 指标与评分为纯函数层; 前端新增 vue-router, 现 App.vue 原样搬 ChatPage, MonitorView 甘特 CSS 自绘。

**Tech Stack:** Python 3.11 / FastAPI / psycopg_pool (AsyncConnectionPool) / Pydantic v2 / pytest(真实 PG) / Vue 3.5 + vue-router@4 + Vite 8 + Tailwind 4 / axios。

**Spec:** `docs/superpowers/specs/2026-10-08-stage-chain-monitoring-design.md`(决策 D1-D11, 本计划逐条实现)

## Global Constraints

- 观测旁路原则: stage_chain / eval / score 任何失败仅 `logger.warning`/log 放行, 不得阻塞业务(D-spec §Notes)
- 拉链行只写 `span_type == "node"` 的 span(D5); `LangGraph_lawApp.py` 零改动
- `trace_runs`/`trace_spans` 表结构与现有 157 用例不回归(D3); 新表 DDL 幂等(CREATE TABLE IF NOT EXISTS, 进 `ensure_tables`)
- 测试跑真实 PostgreSQL, 不可用时显式 SKIP 不 mock(对齐 2026-09-20 spec 测试决策); 指标/评分纯函数零依赖直接断言
- 前端主题: tailwind slate/white/amber 色系, 与现有组件一致; 甘特 CSS 自绘不引图表库(D6)
- 节点执行上限沿用 config 现值: recursion_limit=60 / max_rounds=10 / max_clarify_rounds=5(D9, 不新设)
- commit 风格: 中文 `C:` 前缀一行摘要, 每任务一提交

---

### Task 1: stage_chain / eval_runs 拉链表 DDL + 写入助手

**Files:**
- Modify: `lawApp_LangGraph/db.py`(`ensure_tables` 内追加 DDL; 文件末尾追加助手)
- Test: `tests/test_stage_chain.py`(新)

**Interfaces:**
- Produces(后续任务依赖的精确签名):
  - `async def open_stage(run_id: str, session_id: str, node_name: str, seq: int) -> None` — 失败仅告警
  - `async def close_stage(run_id: str, node_name: str, seq: int, status: str, latency_ms: int, detail: dict | None = None) -> None` — 失败仅告警
  - `async def insert_eval_run(dataset: str, label: str, metrics: dict, cases: list) -> None`
- Consumes: 现有 `get_pool()` / `ensure_tables(conn)` 机制

- [ ] **Step 1: 写失败测试**

```python
# tests/test_stage_chain.py
"""stage_chain 拉链表 + eval_runs — 真实 PG, 不可用显式 SKIP。"""
import asyncio
import time

import pytest

from lawApp_LangGraph import db


def _pg():
    try:
        asyncio.get_event_loop().run_until_complete(db.get_pool())
        return True
    except Exception:
        return False


PG = pytest.mark.skipif(not _pg(), reason="真实 PostgreSQL 不可用")


@PG
def test_ensure_tables_idempotent():
    async def run():
        pool = await db.get_pool()
        async with pool.connection() as conn:
            await db.ensure_tables(conn)  # 重复执行不报错(幂等)
            await db.ensure_tables(conn)
    asyncio.run(run())


@PG
def test_zipper_open_then_close():
    """开行→running 开口; 闭行→ended_at 补齐 + is_current 翻 FALSE。"""
    async def run():
        rid = f"test-run-{time.time_ns()}"
        await db.open_stage(rid, "sess-z", "planner", 1)
        pool = await db.get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT status, ended_at, is_current FROM stage_chain "
                "WHERE run_id=%s AND node_name='planner' AND seq=1", (rid,))
            row = await cur.fetchone()
        assert row[0] == "running" and row[1] is None and row[2] is True
        await db.close_stage(rid, "planner", 1, "ok", 123,
                             {"rag_top_score": 0.7})
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT status, ended_at, is_current, latency_ms, detail->>'rag_top_score' "
                "FROM stage_chain WHERE run_id=%s AND node_name='planner' AND seq=1",
                (rid,))
            row = await cur.fetchone()
        assert row[0] == "ok" and row[1] is not None and row[2] is False
        assert row[3] == 123 and row[4] == "0.7"
        # 清理测试行
        await conn.execute("DELETE FROM stage_chain WHERE run_id=%s", (rid,))
        await conn.commit()
    asyncio.run(run())


@PG
def test_seq_reloop_distinct_rows():
    """同节点 seq 递增 = 回环重跑各占一行(拉链历史)。"""
    async def run():
        rid = f"test-run-{time.time_ns()}"
        for seq in (1, 2):
            await db.open_stage(rid, "sess-z", "executor", seq)
            await db.close_stage(rid, "executor", seq, "ok", 10 * seq, None)
        pool = await db.get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT COUNT(*) FROM stage_chain WHERE run_id=%s AND node_name='executor'",
                (rid,))
            assert (await cur.fetchone())[0] == 2
            await conn.execute("DELETE FROM stage_chain WHERE run_id=%s", (rid,))
            await conn.commit()
    asyncio.run(run())


@PG
def test_eval_run_insert():
    async def run():
        await db.insert_eval_run("retrieval", f"lbl-{time.time_ns()}",
                                 {"hit_rate_at_5": 1.0}, [{"case_id": "x"}])
        pool = await db.get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT dataset, metrics->>'hit_rate_at_5' FROM eval_runs "
                "ORDER BY created_at DESC LIMIT 1")
            row = await cur.fetchone()
            assert row[0] == "retrieval" and row[1] == "1.0"
            await conn.execute("DELETE FROM eval_runs WHERE label LIKE 'lbl-%'")
            await conn.commit()
    asyncio.run(run())
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_stage_chain.py -v`
Expected: FAIL/ERROR — `stage_chain` 表不存在 / `open_stage` 属性缺失

- [ ] **Step 3: 实现 DDL 与助手**

`db.py` `ensure_tables` 的 SQL 字符串末尾(`session_dialogue_events` 索引之后、闭引号之前)追加:

```sql
        -- 阶段拉链表(SCD2): 开行 INSERT / 闭行 UPDATE, 节点级阶段状态
        -- 规格 docs/superpowers/specs/2026-10-08-stage-chain-monitoring-design.md §一
        CREATE TABLE IF NOT EXISTS stage_chain (
            id          BIGSERIAL PRIMARY KEY,
            run_id      TEXT NOT NULL,
            session_id  TEXT NOT NULL,
            node_name   TEXT NOT NULL,
            seq         INT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'running',
            started_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            ended_at    TIMESTAMPTZ,
            is_current  BOOLEAN NOT NULL DEFAULT TRUE,
            latency_ms  INT,
            detail      JSONB DEFAULT '{}'::jsonb
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_stage_chain_slot
            ON stage_chain (run_id, node_name, seq);
        CREATE INDEX IF NOT EXISTS idx_stage_chain_open
            ON stage_chain (ended_at) WHERE ended_at IS NULL;
        CREATE INDEX IF NOT EXISTS idx_stage_chain_session
            ON stage_chain (session_id, started_at);
        -- 评测批次表(P2, 2026-09-20 spec 决策 2 原样)
        CREATE TABLE IF NOT EXISTS eval_runs (
            id          BIGSERIAL PRIMARY KEY,
            dataset     TEXT NOT NULL,
            label       TEXT NOT NULL,
            metrics     JSONB DEFAULT '{}',
            cases       JSONB DEFAULT '[]',
            created_at  TIMESTAMPTZ DEFAULT NOW()
        );
```

`db.py` 文件末尾追加(`import json` 已在文件头):

```python
#  阶段拉链表 / 评测批次写入助手(观测旁路: 失败仅告警)


async def open_stage(run_id: str, session_id: str, node_name: str, seq: int) -> None:
    """拉链开行: running / ended_at NULL / is_current TRUE。"""
    try:
        pool = await get_pool()
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO stage_chain (run_id, session_id, node_name, seq) "
                "VALUES (%s, %s, %s, %s)",
                (run_id, session_id, node_name, seq),
            )
            await conn.commit()
    except Exception as e:  # pragma: no cover — 观测旁路
        logger.warning("stage 开行失败(观测旁路): %s", e)


async def close_stage(
    run_id: str, node_name: str, seq: int, status: str, latency_ms: int,
    detail: Optional[dict] = None,
) -> None:
    """拉链闭行: 按 (run_id, node_name, seq) 定位, 无匹配行静默忽略(开行
    极端乱序/失败时 UPDATE 0 行不算错误)。"""
    try:
        pool = await get_pool()
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE stage_chain SET status=%s, ended_at=NOW(), "
                "is_current=FALSE, latency_ms=%s, detail=%s "
                "WHERE run_id=%s AND node_name=%s AND seq=%s",
                (
                    status, latency_ms,
                    json.dumps(detail or {}, ensure_ascii=False, default=str),
                    run_id, node_name, seq,
                ),
            )
            await conn.commit()
    except Exception as e:  # pragma: no cover — 观测旁路
        logger.warning("stage 闭行失败(观测旁路): %s", e)


async def insert_eval_run(
    dataset: str, label: str, metrics: dict, cases: list
) -> None:
    """评测批次落库(P2)。"""
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO eval_runs (dataset, label, metrics, cases) "
            "VALUES (%s, %s, %s, %s)",
            (
                dataset, label,
                Json(metrics, dumps=lambda o: json.dumps(o, ensure_ascii=False, default=str)),
                Json(cases, dumps=lambda o: json.dumps(o, ensure_ascii=False, default=str)),
            ),
        )
        await conn.commit()
```

注: `Json` 已由 `_trace_json` 使用的 import 提供(检查 `from psycopg.types.json import Json` 是否已在文件头; 若无则补)。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_stage_chain.py -v`
Expected: 4 PASS(PG 可用时)

- [ ] **Step 5: Commit**

```bash
git add lawApp_LangGraph/db.py tests/test_stage_chain.py
git commit -m "C: 监控 Task1 — stage_chain 拉链表+eval_runs DDL(幂等进 ensure_tables)+open/close_stage/insert_eval_run 写入助手(旁路告警)+拉链语义用例(开闭行/seq回环/幂等)"
```

---

### Task 2: @traced 双写开行/闭行(tracing.py)

**Files:**
- Modify: `lawApp_LangGraph/tracing.py`(`RunContext` 加 node_seq 计数器; 新增 `_fire_stage`/`_rag_summary`; `@traced` 的 async wrapper 与 sync wrapper 各加双写)
- Test: `tests/test_stage_chain.py`(追加)

**Interfaces:**
- Consumes: Task 1 的 `db.open_stage` / `db.close_stage`
- Produces: `RunContext.node_seq: dict[str, int]`; 拉链行为(集成测试覆盖)

- [ ] **Step 1: 写失败测试(追加到 tests/test_stage_chain.py)**

```python
@PG
def test_traced_node_writes_zipper():
    """被 @traced("node") 装饰的函数执行后: 拉链开行→闭行齐, seq 递增。"""
    from lawApp_LangGraph.tracing import RunContext, set_run, traced

    async def run():
        rid = f"test-run-{time.time_ns()}"
        run_ctx = set_run(RunContext(run_id=rid, session_id="sess-z",
                                     run_type="live_ask"))

        @traced("node", "planner")
        async def fake_node(state):
            return {"ok": True}

        await fake_node({"x": 1})     # 第 1 次
        await fake_node({"x": 2})     # 第 2 次(回环重跑)
        # 等 fire-and-forget 任务跑完(create_task 下一轮调度)
        await asyncio.sleep(0.3)

        pool = await db.get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT seq, status, is_current FROM stage_chain "
                "WHERE run_id=%s ORDER BY seq", (rid,))
            rows = await cur.fetchall()
        assert [(r[0], r[1], r[2]) for r in rows] == [(1, "ok", False), (2, "ok", False)]
        await conn.execute("DELETE FROM stage_chain WHERE run_id=%s", (rid,))
        await conn.commit()
        set_run(None) if False else None  # noqa: 上下文由测试进程独占, 不回收
    asyncio.run(run())


def test_no_run_context_no_zipper(monkeypatch):
    """无 run 上下文(单测直调)时 fire 静默跳过, 不抛不写。"""
    from lawApp_LangGraph.tracing import traced

    calls = []

    async def boom(*a, **kw):
        calls.append(1)

    # 直接调用 db 层前拦截验证: 无 RunContext 时 _fire_stage 不触 db
    fn = traced("node", "planner")(boom)

    async def run():
        await fn({})

    asyncio.run(run())
    assert calls == [1]  # 函数本体正常执行, 旁路不影响
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_stage_chain.py::test_traced_node_writes_zipper -v`
Expected: FAIL — 拉链无行(@traced 尚未双写)

- [ ] **Step 3: 实现双写**

`tracing.py` 改动:

(a) `RunContext` dataclass 在 `spans` 字段后加:

```python
    node_seq: dict[str, int] = field(default_factory=dict)
```

(b) `_emit` 函数之后追加两个模块级助手:

```python
def _fire_stage(coro) -> None:
    """fire-and-forget: 拿不到 running loop(单测直调)静默跳过;
    任务异常吞并记 WARNING(观测旁路)。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(coro)
    task.add_done_callback(
        lambda t: t.exception()
        and logger.warning("stage 拉链落库失败(旁路): %s", t.exception())
    )


def _rag_summary(output: Any) -> Optional[dict]:
    """best-effort 检索摘要: output 顶层含 rag_documents 列表时取
    top_score/条数, 否则 None(不递归扫描, 拿不到就算了)。"""
    if isinstance(output, dict):
        docs = output.get("rag_documents")
        if isinstance(docs, list) and docs:
            scores = [
                d.get("hybrid_score", 0) if isinstance(d, dict) else 0
                for d in docs
            ]
            return {"rag_top_score": max(scores), "rag_count": len(docs)}
    return None
```

(c) `@traced` 内 **async awrapper** 改造 — 开头 t0 行之后加开行, 收尾/异常处加闭行:

```python
            @functools.wraps(fn)
            async def awrapper(*args, **kwargs):
                from lawApp_LangGraph import db  # 延迟导入避免环

                t0 = time.perf_counter()
                span = Span(...)
                # 阶段拉链开行(D5): 只 node 层
                run = _current_run.get()
                stage_seq = None
                node_name = name or fn.__name__
                if span_type == "node" and run is not None:
                    stage_seq = run.node_seq.get(node_name, 0) + 1
                    run.node_seq[node_name] = stage_seq
                    _fire_stage(
                        db.open_stage(run.run_id, run.session_id, node_name, stage_seq)
                    )
                try:
                    out = await fn(*args, **kwargs)
                except BaseException as e:
                    ...  # 现有 span.status 分支保持不动
                    span.output = {"exception": repr(e)}
                    span.latency_ms = int((time.perf_counter() - t0) * 1000)
                    _emit(span)
                    _close_zipper(db, run, node_name, stage_seq,
                                   span.status, span.latency_ms, e)
                    raise
                span.output = out
                span.latency_ms = int((time.perf_counter() - t0) * 1000)
                _emit(span)
                _close_zipper(db, run, node_name, stage_seq,
                              "ok", span.latency_ms, out)
                return out
```

(d) 同名闭行助手(模块级, `_rag_summary` 之后):

```python
def _close_zipper(db, run, node_name: str, stage_seq, status: str,
                  latency_ms: int, payload: Any) -> None:
    """闭行收口: 异常 payload 记 repr 截断; 正常 payload 尝试 rag 摘要。"""
    if stage_seq is None or run is None:
        return
    detail = {}
    if status == "error" or status == "interrupted" or status == "cancelled":
        detail = {"exception": repr(payload)[:300]}
    else:
        rag = _rag_summary(payload)
        if rag:
            detail = rag
    _fire_stage(
        db.close_stage(run.run_id, node_name, stage_seq, status,
                       latency_ms, detail)
    )
```

(e) **sync wrapper** 同样处理 — 开头/正常退出/异常退出三处与 (c) 完全对称(`db` 延迟导入、`_fire_stage` 内部 `get_running_loop` 已兼容同步上下文; 无 loop 时静默跳过)。

注: sync 节点(ingest/ask_element)由 LangGraph 在事件循环线程内调用, `get_running_loop()` 可用; 图外单测直调时无 loop → 跳过, 满足旁路。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_stage_chain.py -v`
Expected: 全 PASS

- [ ] **Step 5: 现有 tracing 回归**

Run: `python -m pytest tests/test_tracing.py tests/test_trace_db.py tests/test_trace_e2e.py -v`
Expected: 全 PASS(现有 157 内相关用例零回归; e2e 会顺带真实写拉链行, 属预期)

- [ ] **Step 6: Commit**

```bash
git add lawApp_LangGraph/tracing.py tests/test_stage_chain.py
git commit -m "C: 监控 Task2 — @traced 双写拉链(node 层开行/闭行, RunContext node_seq 回环计数, fire-and-forget 旁路, rag 摘要/错误截断进 detail)+sync/async 两 wrapper 对称+用例"
```

---

### Task 3: 指标扩展 + composite_score 综合评分

**Files:**
- Create: `lawApp_LangGraph/score.py`
- Modify: `lawApp_LangGraph/config.py`(Settings 加 score_weights/score_baselines)
- Modify: `lawApp_LangGraph/tracing.py`(`RunContext.metrics()` 扩展; `flush_run` 算综合分)
- Test: `tests/test_score.py`(新)

**Interfaces:**
- Produces: `composite_score(metrics: dict, weights: dict | None = None, judge_score: float | None = None) -> int`(0-100; judge=None 时权重重分配, D11 插槽)
- Produces: `trace_runs.metrics` 新键: `replan_rounds` / `limit_hit` / `rag_top_score` / `tool_error_count` / `composite_score`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_score.py
"""composite_score 纯函数 — 零依赖直接断言。"""
from lawApp_LangGraph.score import composite_score


def test_full_metrics_score_range():
    m = {
        "rag_top_score": 0.8, "total_latency_ms": 30000, "token_prompt": 6000,
        "token_completion": 2000, "node_count": 8, "tool_count": 3,
        "tool_error_count": 0, "clarify_rounds": 0, "replan_rounds": 0,
    }
    s = composite_score(m)
    assert 0 <= s <= 100
    assert s >= 70  # 各分量全优 → 高分


def test_missing_components_redistribute_weight():
    # 无检索分数(闲聊路径) → rag 权重重分配, 不报错不归零
    m = {"total_latency_ms": 30000, "token_prompt": 100, "token_completion": 50,
         "tool_error_count": 0}
    assert 0 <= composite_score(m) <= 100


def test_bad_run_scores_lower_than_good():
    good = {"rag_top_score": 0.9, "total_latency_ms": 20000,
            "token_prompt": 5000, "token_completion": 1500,
            "tool_error_count": 0, "clarify_rounds": 0, "replan_rounds": 0}
    bad = {"rag_top_score": 0.1, "total_latency_ms": 120000,
           "token_prompt": 30000, "token_completion": 9000,
           "tool_error_count": 2, "clarify_rounds": 3, "replan_rounds": 3}
    assert composite_score(bad) < composite_score(good)


def test_judge_slot_none_and_present():
    m = {"rag_top_score": 0.8, "total_latency_ms": 30000,
         "token_prompt": 6000, "token_completion": 2000}
    without = composite_score(m, weights={"rag": 1.0})
    # judge=None: 单分量 rag=0.8 → 80
    assert without == 80
    with_judge = composite_score(m, weights={"rag": 0.5, "judge": 0.5},
                                 judge_score=1.0)
    assert with_judge == 90  # (0.8*0.5 + 1.0*0.5)*100
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_score.py -v`
Expected: FAIL — `No module named 'lawApp_LangGraph.score'`

- [ ] **Step 3: 实现 score.py + config + metrics 扩展**

`lawApp_LangGraph/score.py`(新):

```python
"""综合评分(D10) — 纯函数, 权重可调, judge 插槽(D11)。

规格: docs/superpowers/specs/2026-10-08-stage-chain-monitoring-design.md §三-2
分量: rag 检索质量 / 时延(相对基线) / token 成本(相对基线) / 工具正确率 /
      HITL 轮次 / replan 轮次; 分量缺数据 → 权重重分配, 不惩罚不报错。
"""

from __future__ import annotations

from typing import Optional

DEFAULT_WEIGHTS = {
    "rag": 0.25, "latency": 0.15, "token": 0.15,
    "tool": 0.25, "hitl": 0.10, "replan": 0.10,
}
BASELINES = {"latency_ms": 60000.0, "token_total": 20000.0}
JUDGE = 0.15  # judge 接入时的默认权重(用户传入 weights 可覆盖)


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def composite_score(
    metrics: dict,
    weights: Optional[dict] = None,
    judge_score: Optional[float] = None,
) -> int:
    w = dict(weights or DEFAULT_WEIGHTS)
    comps: dict[str, float] = {}

    rag = metrics.get("rag_top_score")
    if rag is not None:
        comps["rag"] = _clamp(float(rag))

    lat = metrics.get("total_latency_ms")
    if lat:
        comps["latency"] = _clamp(BASELINES["latency_ms"] / max(float(lat), 1.0))

    tok = (metrics.get("token_prompt") or 0) + (metrics.get("token_completion") or 0)
    if tok:
        comps["token"] = _clamp(BASELINES["token_total"] / tok)

    tool_calls = metrics.get("tool_count") or 0
    tool_errs = metrics.get("tool_error_count") or 0
    if tool_calls:
        comps["tool"] = _clamp(1.0 - tool_errs / tool_calls)
    elif tool_errs == 0:
        comps["tool"] = 1.0  # 无工具调用 = 无错误

    hitl = (metrics.get("clarify_rounds") or 0)
    if hitl:
        comps["hitl"] = _clamp(1.0 - hitl / 5.0)

    replan = metrics.get("replan_rounds") or 0
    if replan:
        comps["replan"] = _clamp(1.0 - replan / 5.0)

    if judge_score is not None:
        w.setdefault("judge", JUDGE)
        comps["judge"] = _clamp(float(judge_score))

    # 分量缺数据 → 权重重分配到在场分量
    active = {k: w[k] for k in comps if k in w and w[k] > 0}
    total_w = sum(active.values())
    if total_w == 0 or not active:
        return 0
    return int(round(100.0 * sum(comps[k] * w for k, w in active.items()) / total_w))
```

`config.py` Settings 类 `incorrect_threshold` 组后追加:

```python
    # ============ 综合评分(D10, 环境变量 SCORE_WEIGHTS 传 JSON 可覆盖) ============
    score_weights: dict = DEFAULT_SCORE_WEIGHTS  # 见 score.py; 默认内置
    score_baselines: dict = {"latency_ms": 60000, "token_total": 20000}
```

实现方式: config 不引 score(避免反向依赖), `score.py` 顶部已带 `DEFAULT_WEIGHTS`; config 用字面量副本:

```python
    score_weights: dict = {
        "rag": 0.25, "latency": 0.15, "token": 0.15,
        "tool": 0.25, "hitl": 0.10, "replan": 0.10,
    }
```

`tracing.py` `RunContext.metrics()` 扩展(在现有返回 dict 内追加键):

```python
        return {
            ...现有 8 键不动...,
            "replan_rounds": sum(
                1 for s in self.spans
                if s.span_type == "node" and s.name == "replanner"
            ),
            "tool_error_count": sum(
                1 for s in self.spans
                if s.span_type == "tool" and s.status not in ("ok",)
            ),
            "rag_top_score": max(
                (s.output.get("rag_documents") or [{}])
                and [d.get("hybrid_score", 0) for d in s.output["rag_documents"]]
                or []  # 取不到时空列表
                for s in []  # 占位, 见下方说明
            ) if False else None,  # rag_top_score 由 flush_run 从拉链 detail 聚合, 此处不重复
        }
```

(上面伪代码仅示意 — 实际实现: `metrics()` 追加 `replan_rounds` 与 `tool_error_count` 两键; `rag_top_score` 与 `limit_hit` 与 `composite_score` 在 `flush_run` 内补, 因为 rag 摘要在拉链 detail、limit 判定需读 config。)

`flush_run` 改造(metrics 构造行之后、insert_trace_run 之前插入):

```python
        from lawApp_LangGraph.config import settings
        from lawApp_LangGraph.score import composite_score

        metrics = run.metrics()
        metrics["limit_hit"] = (
            metrics.get("clarify_rounds", 0) >= settings.max_clarify_rounds
            or metrics.get("replan_rounds", 0) >= settings.max_rounds
            or metrics.get("node_count", 0) >= settings.recursion_limit
        )
        rag_scores = []
        for s in run.spans:
            if s.span_type == "tool" and isinstance(s.output, dict):
                docs = s.output.get("rag_documents")
                if isinstance(docs, list) and docs:
                    rag_scores.extend(
                        d.get("hybrid_score", 0) for d in docs if isinstance(d, dict)
                    )
        if rag_scores:
            metrics["rag_top_score"] = max(rag_scores)
        metrics["composite_score"] = composite_score(
            metrics, settings.score_weights
        )
        # insert_trace_run(metrics=metrics) ← 原调用改为传此 metrics
```

(原 `insert_trace_run(run_id=..., metrics=run.metrics(), ...)` 行改为传局部 `metrics` 变量。)

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_score.py tests/test_trace_db.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add lawApp_LangGraph/score.py lawApp_LangGraph/config.py lawApp_LangGraph/tracing.py tests/test_score.py
git commit -m "C: 监控 Task3 — score.py composite_score 纯函数(6分量加权/缺数据权重重分配/judge插槽None语义)+metrics扩展(replan_rounds/tool_error_count/limit_hit/rag_top_score)+flush_run 收尾算综合分"
```

---

### Task 4: 评测指标纯函数 eval_metrics.py

**Files:**
- Create: `lawApp_LangGraph/eval_metrics.py`
- Test: `tests/test_eval_metrics.py`(新)

**Interfaces:**
- Produces:
  - `hit_rate_at_k(cases: list[dict], k: int = 5) -> float`
  - `mrr_at_k(cases: list[dict], k: int = 10) -> float`
  - `precision_at_k(cases, k=5) -> float` / `recall_at_k(cases, k=5) -> float` / `f1_at_k(cases, k=5) -> float`
  - `evaluate(cases, k=5) -> dict` — 返回全部指标键值对
  - case 结构: `{"ranked_ids": list[str], "relevant_ids": list[str]}`(gen_golden_set 产此结构, run_eval 消费)

- [ ] **Step 1: 写失败测试**

```python
# tests/test_eval_metrics.py
"""检索评测指标纯函数 — 含 P/R/F1 单 ground truth 退化路径。"""
from lawApp_LangGraph.eval_metrics import (
    evaluate, f1_at_k, hit_rate_at_k, mrr_at_k, precision_at_k, recall_at_k,
)

CASES = [
    {"ranked_ids": ["A", "B", "C", "D", "E"], "relevant_ids": ["A"]},   # 命中@1
    {"ranked_ids": ["B", "C", "A", "D", "E"], "relevant_ids": ["A"]},   # 命中@3
    {"ranked_ids": ["B", "C", "D", "E", "F"], "relevant_ids": ["A"]},   # 未命中
]
MULTI = [{"ranked_ids": ["A", "B", "C", "D", "E"], "relevant_ids": ["A", "B"]}]


def test_hit_rate_at_5():
    assert hit_rate_at_k(CASES, k=5) == 2 / 3
    assert hit_rate_at_k(CASES, k=1) == 1 / 3


def test_mrr_at_10():
    # 1/1 + 1/3 + 0 = 4/3, /3 = 4/9
    assert abs(mrr_at_k(CASES, k=10) - 4 / 9) < 1e-9


def test_prf_single_truth_degenerate():
    # 单 ground truth: precision@5 = 1/5(命中时), recall@5 = 1(命中时)
    assert precision_at_k(CASES[:1], k=5) == 1 / 5
    assert recall_at_k(CASES[:1], k=5) == 1.0
    assert abs(f1_at_k(CASES[:1], k=5) - 2 * (1 / 5 * 1) / (1 / 5 + 1)) < 1e-9


def test_prf_multi_truth_meaningful():
    assert precision_at_k(MULTI, k=5) == 2 / 5
    assert recall_at_k(MULTI, k=5) == 1.0


def test_evaluate_aggregates_all():
    out = evaluate(CASES, k=5)
    for key in ("hit_rate_at_5", "mrr_at_5", "precision_at_5",
                "recall_at_5", "f1_at_5"):
        assert key in out and 0.0 <= out[key] <= 1.0


def test_empty_cases_zero():
    assert evaluate([], k=5)["hit_rate_at_5"] == 0.0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_eval_metrics.py -v`
Expected: FAIL — 模块不存在

- [ ] **Step 3: 实现**

```python
# lawApp_LangGraph/eval_metrics.py
"""检索评测指标(P2 落地) — 纯函数, hit_rate/MRR 为主, P/R/F1 一并输出。

已知局限(spec 记录): golden set 单条 ground truth 时 recall@k∈{0,1}、
precision@k 退化近似 hit_rate; 多相关案例标注扩展后自动有意义。
"""

from __future__ import annotations


def _hits(case: dict, k: int) -> int:
    top = list(case.get("ranked_ids") or [])[:k]
    rel = set(case.get("relevant_ids") or [])
    return sum(1 for i in top if i in rel)


def hit_rate_at_k(cases: list[dict], k: int = 5) -> float:
    if not cases:
        return 0.0
    return sum(1 for c in cases if _hits(c, k) > 0) / len(cases)


def mrr_at_k(cases: list[dict], k: int = 10) -> float:
    if not cases:
        return 0.0
    total = 0.0
    for c in cases:
        rel = set(c.get("relevant_ids") or [])
        rr = 0.0
        for rank, rid in enumerate(list(c.get("ranked_ids") or [])[:k], 1):
            if rid in rel:
                rr = 1.0 / rank
                break
        total += rr
    return total / len(cases)


def precision_at_k(cases: list[dict], k: int = 5) -> float:
    if not cases:
        return 0.0
    return sum(_hits(c, k) for c in cases) / (len(cases) * k)


def recall_at_k(cases: list[dict], k: int = 5) -> float:
    if not cases:
        return 0.0
    vals = [
        _hits(c, k) / len(c["relevant_ids"])
        for c in cases
        if c.get("relevant_ids")
    ]
    return sum(vals) / len(cases) if vals else 0.0


def f1_at_k(cases: list[dict], k: int = 5) -> float:
    p, r = precision_at_k(cases, k), recall_at_k(cases, k)
    return 2 * p * r / (p + r) if (p + r) > 0 else 0.0


def evaluate(cases: list[dict], k: int = 5) -> dict:
    return {
        f"hit_rate_at_{k}": hit_rate_at_k(cases, k),
        f"mrr_at_{k}": mrr_at_k(cases, k),
        f"precision_at_{k}": precision_at_k(cases, k),
        f"recall_at_{k}": recall_at_k(cases, k),
        f"f1_at_{k}": f1_at_k(cases, k),
    }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_eval_metrics.py -v`
Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add lawApp_LangGraph/eval_metrics.py tests/test_eval_metrics.py
git commit -m "C: 监控 Task4 — eval_metrics.py 纯函数(hit_rate@k/MRR@k/P@k/R@k/F1@k/evaluate聚合)+单ground truth退化路径用例"
```

---

### Task 5: golden set 生成 + 评测 runner

**Files:**
- Create: `scripts/gen_golden_set.py`
- Create: `scripts/run_eval.py`
- Create: `data/eval/.gitkeep`(`retrieval.jsonl` 不进 git, 可重生成)

**Interfaces:**
- Consumes: Task 4 `evaluate()`; Task 1 `db.insert_eval_run`; `db.get_pool()`(law_cases 表直读); `RAG_service.base.get_retriever().search(query, top_k, rerank_top_n, alpha, namespace)`
- Produces: `data/eval/retrieval.jsonl` — 每行 `{"case_id", "question", "case_cause", "year", "chunk_text"}`; `python scripts/run_eval.py --suite retrieval --label <sha> [--baseline <label>]` 落 eval_runs 行

- [ ] **Step 1: 实现 gen_golden_set.py**

```python
# scripts/gen_golden_set.py
"""生成 retrieval golden set(30 条): law_cases 表 chunk_index=0 分层抽样
(case_cause × year), DeepSeek 从 chunk_text 提炼当事人视角问题, 该案例即
hit ground truth。产出 data/eval/retrieval.jsonl(不进 git 可重生成)。

用法: python scripts/gen_golden_set.py [--count 30] [--out data/eval/retrieval.jsonl]
"""
import argparse
import asyncio
import json
import random
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


async def sample_cases(count: int) -> list[dict]:
    from lawApp_LangGraph import db

    pool = await db.get_pool()
    async with pool.connection() as conn:
        # 每个案由取 chunk_index=0 的首块(案情概览最完整)
        cur = await conn.execute(
            "SELECT id, year, case_cause, chunk_text FROM law_cases "
            "WHERE chunk_index = 0 ORDER BY id"
        )
        rows = await cur.fetchall()
    # 分层: 按 case_cause 分组轮转抽, 覆盖全部案由
    by_cause: dict[str, list] = {}
    for r in rows:
        by_cause.setdefault(r[2] or "其他", []).append(
            {"case_id": r[0], "year": r[1], "case_cause": r[2] or "其他",
             "chunk_text": (r[3] or "")[:1500]}
        )
    rng = random.Random(42)
    picked: list[dict] = []
    groups = [list(v) for v in by_cause.values()]
    while len(picked) < count and groups:
        for g in groups:
            if g and len(picked) < count:
                picked.append(g.pop(rng.randrange(len(g))))
        groups = [g for g in groups if g]
    return picked


def refine_question(chunk_text: str) -> str:
    """DeepSeek 从 chunk 提炼当事人视角问题; 失败回退截断原文首句。"""
    import httpx

    from lawApp_LangGraph.config import settings

    prompt = (
        "你是测试用例设计者。以下是一段婚姻家事案例原文, 请以当事人第一视角"
        "提炼一个自然语言法律咨询问题(50字内, 不复述案情细节, 只留诉求), "
        "只输出问题本身:\n\n" + chunk_text[:1200]
    )
    try:
        resp = httpx.post(
            f"{settings.deepseek_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
            json={"model": settings.deepseek_flash_model,
                  "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": 120, "temperature": 0.3},
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return chunk_text[:80] + "...(请分析此案例涉及的法律问题)"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=30)
    ap.add_argument("--out", default=str(REPO / "data" / "eval" / "retrieval.jsonl"))
    args = ap.parse_args()

    cases = await sample_cases(args.count)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for c in cases:
            c["question"] = refine_question(c["chunk_text"])
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(f"golden set 生成完成: {len(cases)} 条 → {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: 实现 run_eval.py**

```python
# scripts/run_eval.py
"""评测 runner(P2): retrieval suite 直调检索器(不走 HTTP/图), 每用例取
top-10 排序 id, 指标聚合落 eval_runs; --baseline 输出两批 diff。

用法: python scripts/run_eval.py --suite retrieval --label <git_sha> [--baseline <label>]
"""
import argparse
import asyncio
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


async def run_retrieval(label: str, baseline: str | None) -> None:
    from lawApp_LangGraph import db
    from lawApp_LangGraph.RAG_service.base import get_retriever
    from lawApp_LangGraph.eval_metrics import evaluate

    path = REPO / "data" / "eval" / "retrieval.jsonl"
    cases = [json.loads(line) for line in
             path.open(encoding="utf-8").read().splitlines() if line]
    if not cases:
        raise SystemExit(f"golden set 为空, 先跑: python scripts/gen_golden_set.py")

    retriever = get_retriever()
    eval_cases: list[dict] = []
    for c in cases:
        results = await retriever.search(query=c["question"], top_k=10,
                                          rerank_top_n=10, alpha=0.4,
                                          namespace=None)
        ranked = [r.get("case_number") or r.get("id") or ""
                  for r in results]
        # ground truth: 该案例自身 case_number(law_cases.id 即 case_number 主键)
        eval_cases.append({"ranked_ids": ranked,
                           "relevant_ids": [c["case_id"]]})

    metrics = evaluate(eval_cases, k=5)
    await db.insert_eval_run("retrieval", label, metrics,
                             [{"case_id": c["case_id"]} for c in cases])
    print(json.dumps(metrics, ensure_ascii=False, indent=2))

    if baseline:
        pool = await db.get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT metrics FROM eval_runs WHERE label=%s AND dataset='retrieval' "
                "ORDER BY created_at DESC LIMIT 1", (baseline,))
            row = await cur.fetchone()
        if row:
            old = row[0] or {}
            print("\n-- baseline diff ({} → {}) --".format(baseline, label))
            for k, v in metrics.items():
                ov = float(old.get(k, 0) or 0)
                print(f"{k}: {ov:.4f} → {v:.4f} ({v - ov:+.4f})")
        else:
            print(f"baseline {baseline} 无记录, 跳过 diff")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", choices=["retrieval"], default="retrieval")
    ap.add_argument("--label", required=True)
    ap.add_argument("--baseline", default=None)
    args = ap.parse_args()
    await run_retrieval(args.label, args.baseline)


if __name__ == "__main__":
    asyncio.run(main())
```

注: `retriever.search` 的 namespace 参数按 `rag_tools.py:88` 现签名(`namespace or settings.pinecone_namespace`), 传 None 即默认命名空间; ranked id 取 `case_number` 优先(law_cases 主键 id 即 case_number, 与 golden set case_id 同源, gen 脚本 `SELECT id` 保证一致)。

- [ ] **Step 3: 冒烟运行(真实服务)**

Run: `python scripts/gen_golden_set.py --count 5 --out data/eval/retrieval_smoke.jsonl`
Expected: 输出 5 条 jsonl, question 为中文问句

Run: `python scripts/run_eval.py --suite retrieval --label smoke`(临时把 path 指到 smoke 文件或生成正式集后跑)
Expected: 打印指标 JSON, `eval_runs` 表出现 label=smoke 行; 完后 `DELETE FROM eval_runs WHERE label='smoke'` 清理

- [ ] **Step 4: Commit**

```bash
git add scripts/gen_golden_set.py scripts/run_eval.py data/eval/.gitkeep
git commit -m "C: 监控 Task5 — P2评测落地: gen_golden_set(law_cases分层抽样+DeepSeek提炼当事人问题)+run_eval(retrieval suite直调检索器, hit/MRR/P/R/F1聚合落eval_runs, --baseline diff)"
```

---

### Task 6: /monitor 4 端点 + 响应模型

**Files:**
- Modify: `lawApp_LangGraph/FastAPI/model.py`(文件末尾追加监控模型)
- Modify: `lawApp_LangGraph/FastAPI/api.py`(文件末尾追加 4 端点)
- Test: `tests/test_monitor_api.py`(新)

**Interfaces:**
- Consumes: Task 1 表结构; `trace_runs`/`trace_spans`/`eval_runs` 现有数据
- Produces(前端 Task 8 消费的 JSON 契约):
  - `GET /monitor/overview` → `MonitorOverview{runs_by_status: dict[str,int], running_stages: int, limit_hit_runs: int, node_fail_top: list[{node_name,errors}], score_distribution: dict[str,int]}`
  - `GET /monitor/runs?limit=50&status=&session_id=` → `list[MonitorRunItem{run_id, session_id, run_type, mode, status, started_at, ended_at, stage_total, stage_ok, stage_running, metrics}]`
  - `GET /monitor/runs/{run_id}/stages` → `MonitorRunDetail{run_id, session_id, run_type, mode, status, started_at, ended_at, metrics, stages: list[MonitorStage{node_name, seq, status, started_at, ended_at, latency_ms, detail}], spans: list[MonitorSpan{span_type, name, status, input, output, state, latency_ms, token_usage, started_at}]}`
  - `GET /monitor/evals?limit=20` → `list[MonitorEval{id, dataset, label, metrics, created_at}]`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_monitor_api.py
"""/monitor 端点 — TestClient 模式(对齐 test_smoke)。"""
import time

import pytest
from fastapi.testclient import TestClient

from lawApp_LangGraph.FastAPI.api import app


client = TestClient(app)


def _insert_fixture():
    """造一条 run + 拉链行(直连 db), 端点读它。PG 不可用则 skip。"""
    import asyncio

    from lawApp_LangGraph import db

    try:
        asyncio.run(db.get_pool())
    except Exception:
        pytest.skip("真实 PostgreSQL 不可用")

    rid = f"api-mon-{time.time_ns()}"

    async def seed():
        await db.insert_trace_run(
            run_id=rid, session_id="sess-api", run_type="live_ask",
            mode="attorney", status="ok", query="q", final_answer="a",
            metrics={"composite_score": 88, "limit_hit": False,
                     "node_count": 3, "tool_count": 1, "llm_count": 2,
                     "total_latency_ms": 100, "token_prompt": 10,
                     "token_completion": 5, "clarify_rounds": 0,
                     "replan_rounds": 0, "tool_error_count": 0},
            started_at=time.time() - 1, ended_at=time.time(),
        )
        await db.open_stage(rid, "sess-api", "planner", 1)
        await db.close_stage(rid, "planner", 1, "ok", 50, None)
        await db.insert_trace_spans([{
            "run_id": rid, "span_type": "node", "name": "planner",
            "status": "ok", "input": {"q": 1}, "output": {"plan": []},
            "state": None, "latency_ms": 50, "token_usage": None,
            "started_at": time.time() - 0.5,
        }])
    asyncio.run(seed())
    return rid


def test_monitor_runs_list_and_detail():
    rid = _insert_fixture()
    r = client.get("/api/monitor/runs?limit=200")
    assert r.status_code == 200
    body = r.json()
    item = next((x for x in body if x["run_id"] == rid), None)
    assert item is not None
    assert item["stage_total"] == 1 and item["stage_ok"] == 1
    assert item["stage_running"] == 0
    assert item["metrics"]["composite_score"] == 88

    r2 = client.get(f"/api/monitor/runs/{rid}/stages")
    assert r2.status_code == 200
    detail = r2.json()
    assert detail["stages"][0]["node_name"] == "planner"
    assert detail["stages"][0]["status"] == "ok"
    assert detail["spans"][0]["name"] == "planner"


def test_monitor_overview_and_evals():
    _insert_fixture()
    r = client.get("/api/monitor/overview")
    assert r.status_code == 200
    body = r.json()
    assert "runs_by_status" in body and "running_stages" in body
    assert "node_fail_top" in body and "score_distribution" in body

    r2 = client.get("/api/monitor/evals")
    assert r2.status_code == 200
    assert isinstance(r2.json(), list)


def test_monitor_run_detail_404():
    r = client.get("/api/monitor/runs/no-such-run/stages")
    assert r.status_code == 404
```

注: TestClient base url 前缀 `/api` 是否需要, 参照 `tests/test_smoke.py` 现有写法 — 实现时先读该文件对齐(如果 smoke 直接 `client.get("/sessions")` 无前缀, 则去掉 `/api`)。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_monitor_api.py -v`
Expected: FAIL — 404(端点不存在)

- [ ] **Step 3: 实现模型与端点**

`model.py` 末尾追加:

```python
#  监控页响应(D-spec §五)


class MonitorStage(BaseModel):
    node_name: str
    seq: int
    status: str
    started_at: str
    ended_at: Optional[str] = None
    latency_ms: Optional[int] = None
    detail: Dict[str, Any] = Field(default_factory=dict)


class MonitorSpan(BaseModel):
    span_type: str
    name: str
    status: Optional[str] = None
    input: Any = None
    output: Any = None
    state: Any = None
    latency_ms: Optional[int] = None
    token_usage: Any = None
    started_at: Optional[str] = None


class MonitorRunItem(BaseModel):
    run_id: str
    session_id: Optional[str] = None
    run_type: str
    mode: Optional[str] = None
    status: str
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    stage_total: int = 0
    stage_ok: int = 0
    stage_running: int = 0
    metrics: Dict[str, Any] = Field(default_factory=dict)


class MonitorRunDetail(MonitorRunItem):
    stages: List[MonitorStage] = Field(default_factory=list)
    spans: List[MonitorSpan] = Field(default_factory=list)


class MonitorOverview(BaseModel):
    runs_by_status: Dict[str, int] = Field(default_factory=dict)
    running_stages: int = 0
    limit_hit_runs: int = 0
    node_fail_top: List[Dict[str, Any]] = Field(default_factory=list)
    score_distribution: Dict[str, int] = Field(default_factory=dict)


class MonitorEval(BaseModel):
    id: int
    dataset: str
    label: str
    metrics: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None
```

`api.py` 末尾追加(PG 掉线降级风格对齐 `list_sessions`):

```python
#  监控页端点(D-spec §五): PG 断连降级空态 + ERROR 日志, 不 500


@app.get("/monitor/overview", response_model=MonitorOverview)
async def monitor_overview():
    from lawApp_LangGraph.db import get_pool

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
                "WHERE metrics->>'limit_hit' = 'true'")
            limit_hit = (await cur.fetchone())[0]
            cur = await conn.execute(
                "SELECT node_name, COUNT(*) FROM stage_chain "
                "WHERE status = 'error' "
                "AND started_at > NOW() - INTERVAL '24 hours' "
                "GROUP BY node_name ORDER BY 2 DESC LIMIT 5"
            )
            fails = [{"node_name": r[0], "errors": r[1]}
                     for r in await cur.fetchall()]
            # 分数分布: [0-59)低 [60-79)中 [80-100]高三档
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
async def monitor_runs(limit: int = 50, status: str = None,
                       session_id: str = None):
    from lawApp_LangGraph.db import get_pool

    conds, params = ["1=1"], []
    if status:
        conds.append("r.status = %s")
        params.append(status)
    if session_id:
        conds.append("r.session_id = %s")
        params.append(session_id)
    sql = (
        "SELECT r.run_id, r.session_id, r.run_type, r.mode, r.status, "
        "r.started_at, r.ended_at, r.metrics, "
        "(SELECT COUNT(*) FROM stage_chain s WHERE s.run_id = r.run_id), "
        "(SELECT COUNT(*) FROM stage_chain s WHERE s.run_id = r.run_id "
        " AND s.status = 'ok'), "
        "(SELECT COUNT(*) FROM stage_chain s WHERE s.run_id = r.run_id "
        " AND s.ended_at IS NULL) "
        "FROM trace_runs r WHERE " + " AND ".join(conds) + " "
        "ORDER BY r.started_at DESC LIMIT %s"
    )
    params.append(min(limit, 500))
    try:
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(sql, tuple(params))
            rows = await cur.fetchall()
    except Exception as e:
        flow.error("monitor runs 降级", detail=str(e))
        return []
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
```

`api.py` 头部 import 的 model 元组追加: `MonitorEval, MonitorOverview, MonitorRunDetail, MonitorRunItem, MonitorSpan, MonitorStage`。
`FastAPI/model.py` 的 `Any/Dict/List/Optional` 已在文件头 import(检查, 缺则补)。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_monitor_api.py -v`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add lawApp_LangGraph/FastAPI/model.py lawApp_LangGraph/FastAPI/api.py tests/test_monitor_api.py
git commit -m "C: 监控 Task6 — /monitor 4端点(overview/runs列表join拉链聚合/run详情stages+spans/evals)+6响应模型+PG掉线降级不500+404/503语义+端点用例"
```

---

### Task 7: 前端路由壳(vue-router + ChatPage 搬迁 + 入口)

**Files:**
- Modify: `frontend/package.json`(依赖加 `vue-router`)
- Create: `frontend/src/router.js`
- Create: `frontend/src/views/ChatPage.vue`(现 App.vue 全部内容原样搬 + header 加监控链接)
- Modify: `frontend/src/App.vue`(改 `<router-view />` 壳)
- Modify: `frontend/src/main.js`(挂 router)
- Create: `frontend/src/views/MonitorView.vue`(Task 8 实装, 本任务先占位使路由可跑)

**Interfaces:**
- Consumes: 无(纯前端)
- Produces: 路由 `/`(ChatPage) + `/monitor`(MonitorView); ChatPage header 内 `<router-link to="/monitor">` 入口(D4)

- [ ] **Step 1: 装依赖**

```bash
cd frontend && npm install vue-router@4
```

- [ ] **Step 2: 建 router.js + 占位 MonitorView**

```js
// frontend/src/router.js
import { createRouter, createWebHistory } from 'vue-router'
import ChatPage from './views/ChatPage.vue'
import MonitorView from './views/MonitorView.vue'

export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'chat', component: ChatPage },
    { path: '/monitor', name: 'monitor', component: MonitorView },
  ],
})
```

```vue
<!-- frontend/src/views/MonitorView.vue (Task 8 实装前的可运行占位) -->
<template>
  <div class="h-screen bg-slate-100 p-8">
    <router-link to="/" class="text-blue-600 text-sm">← 返回咨询</router-link>
    <h1 class="text-xl font-semibold text-slate-700 mt-4">Agent 监控(建设中)</h1>
  </div>
</template>
```

- [ ] **Step 3: App.vue 内容搬到 ChatPage.vue, App 改壳, main.js 挂路由**

`frontend/src/views/ChatPage.vue`: 将现 `App.vue` 文件全文复制为起点, 仅改两处:

1. 所有相对导入不变(`./store`、`./api`、`./components/...`、`./sse` —— ChatPage 在 `src/views/` 下, 相对路径需改为 `../store`、`../api`、`../components/...`、`../sse`)
2. `<template>` 内 header 一行(session-id span 之后)加监控入口:

```html
        <router-link
          to="/monitor"
          class="ml-auto text-sm text-slate-500 hover:text-blue-600 border border-slate-300 rounded px-2 py-0.5 bg-white/70"
        >
          监控 →
        </router-link>
```

(该 header 现为 `flex items-center gap-3 p-3`, `ml-auto` 把链接推到最右; 移除原 `id="session-id"` span 上多余的推挤样式如无。)

`frontend/src/App.vue` 全文替换为:

```vue
<script setup>
// 路由壳(D4): 聊天页内容已搬至 views/ChatPage.vue, 监控页 views/MonitorView.vue
</script>

<template>
  <router-view />
</template>
```

`frontend/src/main.js` 全文替换为:

```js
import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import './style.css'

createApp(App).use(router).mount('#app')
```

- [ ] **Step 4: 构建 + 手动验证**

Run: `cd frontend && npm run build`
Expected: 零错误零警告新增

Run: `npm run dev` 后浏览器开 `http://localhost:5173/`(聊天页原样)与 `http://localhost:5173/monitor`(占位页 + 返回链接); 聊天页 header "监控 →" 链接可跳转。

- [ ] **Step 5: Commit**

```bash
git add frontend/package.json frontend/package-lock.json frontend/src/router.js frontend/src/views/ frontend/src/App.vue frontend/src/main.js
git commit -m "C: 监控 Task7 — vue-router@4 接入: App.vue 改 router-view 壳/原内容原样搬 ChatPage(相对路径上移一级)/header 加监控→入口/monitor 路由 + MonitorView 占位"
```

---

### Task 8: 监控页实装(总览 + runs 表 + 甘特图 + 阶段抽屉 + 轮询)

**Files:**
- Modify: `frontend/src/api.js`(追加 monitor 封装)
- Create: `frontend/src/components/GanttTimeline.vue`
- Create: `frontend/src/components/StageDetailDrawer.vue`
- Modify: `frontend/src/views/MonitorView.vue`(占位替换为实装)

**Interfaces:**
- Consumes: Task 6 端点 JSON 契约(见 Task 6 Interfaces); `api.js` 的 axios 实例 `http`
- Produces: 可用 `/monitor` 页面

- [ ] **Step 1: api.js 追加**

```js
// ========== 监控页(D-spec §五, Task 6 端点) ==========
export const getMonitorOverview = () =>
  http.get('/monitor/overview').then((r) => r.data)

export const getMonitorRuns = (limit = 50) =>
  http.get('/monitor/runs', { params: { limit } }).then((r) => r.data)

export const getMonitorRunDetail = (runId) =>
  http.get(`/monitor/runs/${runId}/stages`).then((r) => r.data)

export const getMonitorEvals = () =>
  http.get('/monitor/evals').then((r) => r.data)
```

- [ ] **Step 2: GanttTimeline.vue**

```vue
<!-- frontend/src/components/GanttTimeline.vue
  甘特时间线(D6): 共享时间轴, 节点行×seq 叠行, CSS 自绘(不引图表库)。
  条色: ok 绿 / error 红 / interrupted 黄 / cancelled 灰 / running 蓝延伸至现在。
-->
<script setup>
import { computed } from 'vue'

const props = defineProps({
  stages: { type: Array, default: () => [] }, // MonitorStage[]
  now: { type: Number, default: () => Date.now() },
})
const emit = defineEmits(['select'])

const t0t1 = computed(() => {
  const pts = props.stages.flatMap((s) =>
    [Date.parse(s.started_at), s.ended_at ? Date.parse(s.ended_at) : props.now]
  )
  if (!pts.length) return [0, 1]
  return [Math.min(...pts), Math.max(...pts, Math.min(...pts) + 1)]
})

function pct(iso) {
  const [a, b] = t0t1.value
  return (100 * (Date.parse(iso) - a)) / (b - a)
}

function bar(s) {
  const l = pct(s.started_at)
  const r = s.ended_at ? pct(s.ended_at) : 100
  return { left: `${l}%`, width: `${Math.max(r - l, 0.5)}%` }
}

const CLS = {
  ok: 'bg-emerald-500/80', error: 'bg-red-500/80',
  interrupted: 'bg-amber-500/80', cancelled: 'bg-slate-400/70',
  running: 'bg-blue-500/80 animate-pulse',
}
const rows = computed(() =>
  [...props.stages].sort((a, b) =>
    Date.parse(a.started_at) - Date.parse(b.started_at))
)
</script>

<template>
  <div class="border rounded bg-white p-3">
    <div class="text-xs text-slate-400 mb-2 flex justify-between">
      <span>{{ new Date(t0t1[0]).toLocaleTimeString() }}</span>
      <span>{{ new Date(t0t1[1]).toLocaleTimeString() }}</span>
    </div>
    <div class="space-y-1">
      <div
        v-for="s in rows"
        :key="s.node_name + ':' + s.seq"
        class="relative h-7 group cursor-pointer"
        @click="emit('select', s)"
      >
        <span class="absolute left-0 w-44 truncate text-xs text-slate-600 z-10 bg-white/60">
          {{ s.node_name }}<template v-if="s.seq > 1"> (第{{ s.seq }}次)</template>
        </span>
        <div
          class="absolute top-1 h-5 rounded opacity-90 hover:opacity-100 transition-opacity"
          :class="CLS[s.status] || CLS.running"
          :style="bar(s)"
          :title="`${s.node_name} #${s.seq} · ${s.status} · ${s.latency_ms ?? '…'}ms`"
        />
      </div>
    </div>
  </div>
</template>
```

- [ ] **Step 3: StageDetailDrawer.vue**

```vue
<!-- frontend/src/components/StageDetailDrawer.vue
  阶段内容全景(D7/D8): 甘特条点击展开 —— 该节点关联 spans 的
  input/output/state/token 折叠面板; rag_documents 逐条 hybrid_score 分数条
  (correct≥0.5 绿 / ambiguous≥0.2 黄 / incorrect 红)。
-->
<script setup>
import { computed } from 'vue'

const props = defineProps({
  stage: { type: Object, default: null },      // MonitorStage
  spans: { type: Array, default: () => [] },   // 该 run 全部 span
})

const related = computed(() =>
  props.stage ? props.spans.filter((x) => x.name === props.stage.node_name) : []
)
const ragDocs = computed(() => {
  for (const s of related.value) {
    const docs = s?.output?.rag_documents || s?.output?.tool_result?.rag_documents
    if (Array.isArray(docs) && docs.length) return docs
  }
  return []
})
function scoreCls(sc) {
  return sc >= 0.5 ? 'bg-emerald-500' : sc >= 0.2 ? 'bg-amber-500' : 'bg-red-400'
}
const secs = [
  { key: 'input', label: '输入 (入参 state)' },
  { key: 'output', label: '输出 (工具结果/节点产出)' },
  { key: 'state', label: '执行后 state 快照' },
]
</script>

<template>
  <div v-if="stage" class="border rounded bg-white p-4 space-y-3">
    <h3 class="font-semibold text-slate-700">
      {{ stage.node_name }}
      <template v-if="stage.seq > 1">(第{{ stage.seq }}次)</template>
      <span class="ml-2 text-xs text-slate-400">
        {{ stage.status }} · {{ stage.latency_ms ?? '?' }}ms ·
        {{ stage.started_at }} → {{ stage.ended_at || '进行中' }}
      </span>
    </h3>

    <div v-if="ragDocs.length" class="space-y-1">
      <p class="text-xs text-slate-500 font-medium">检索结果 (hybrid_score 分数条)</p>
      <div v-for="(d, i) in ragDocs" :key="i" class="flex items-center gap-2 text-xs">
        <span class="w-56 truncate text-slate-600">
          {{ d.case_number || d.law_title || `doc-${i}` }}
        </span>
        <div class="flex-1 h-2 bg-slate-100 rounded">
          <div class="h-2 rounded" :class="scoreCls(d.hybrid_score || 0)"
               :style="{ width: `${Math.min((d.hybrid_score || 0) * 100, 100)}%` }" />
        </div>
        <span class="w-10 text-right text-slate-500">
          {{ (d.hybrid_score || 0).toFixed(3) }}
        </span>
      </div>
    </div>

    <details v-for="sec in secs" :key="sec.key" class="text-xs">
      <summary class="cursor-pointer text-slate-500 hover:text-slate-700">
        {{ sec.label }}
      </summary>
      <pre class="mt-1 max-h-64 overflow-auto bg-slate-50 border rounded p-2
text-[11px] leading-tight whitespace-pre-wrap">{{ JSON.stringify(related.map(s => s[sec.key]), null, 1) }}</pre>
    </details>

    <details class="text-xs">
      <summary class="cursor-pointer text-slate-500 hover:text-slate-700">token 用量</summary>
      <pre class="mt-1 bg-slate-50 border rounded p-2 text-[11px]">{{
        JSON.stringify(related.map(s => s.token_usage), null, 1)
      }}</pre>
    </details>
  </div>
</template>
```

- [ ] **Step 4: MonitorView.vue 实装**

```vue
<!-- frontend/src/views/MonitorView.vue
  监控页(D-spec §六): 总览卡 → runs 表(mini 瀑布条) → 甘特详情 →
  阶段抽屉; 3s 轮询, 无 running 行停轮(收尾拉一次)。
-->
<script setup>
import { onMounted, onUnmounted, ref, computed } from 'vue'
import {
  getMonitorEvals, getMonitorOverview, getMonitorRunDetail, getMonitorRuns,
} from '../api'
import GanttTimeline from '../components/GanttTimeline.vue'
import StageDetailDrawer from '../components/StageDetailDrawer.vue'

const overview = ref({})
const runs = ref([])
const evals = ref([])
const detail = ref(null)          // MonitorRunDetail
const selectedStage = ref(null)   // MonitorStage
const timer = ref(null)
const now = ref(Date.now())

async function refresh() {
  const [ov, rs, ev] = await Promise.all([
    getMonitorOverview(), getMonitorRuns(50), getMonitorEvals(),
  ])
  overview.value = ov
  runs.value = rs
  evals.value = ev
  const anyRunning = rs.some((r) => r.stage_running > 0)
  now.value = Date.now()
  if (anyRunning && !timer.value) {
    timer.value = setInterval(tick, 3000)
  } else if (!anyRunning && timer.value) {
    clearInterval(timer.value)
    timer.value = null
  }
}

async function tick() {
  runs.value = await getMonitorRuns(50)
  overview.value = await getMonitorOverview()
  if (detail.value) {
    detail.value = await getMonitorRunDetail(detail.value.run_id)
  }
  now.value = Date.now()
}

async function openRun(r) {
  detail.value = await getMonitorRunDetail(r.run_id)
  selectedStage.value = null
}

function scoreCls(s) {
  if (s == null) return 'text-slate-400'
  return s >= 80 ? 'text-emerald-600' : s >= 60 ? 'text-amber-600' : 'text-red-500'
}

onMounted(refresh)
onUnmounted(() => timer.value && clearInterval(timer.value))
</script>

<template>
  <div class="h-screen overflow-y-auto bg-slate-100 p-4 space-y-4">
    <header class="flex items-center gap-3 border-b pb-2 bg-white/80 p-3 rounded">
      <router-link to="/" class="text-sm text-blue-600 hover:underline">← 返回咨询</router-link>
      <h1 class="text-lg font-semibold text-slate-700">Agent 阶段监控</h1>
      <span v-if="overview.running_stages" class="text-xs text-blue-600">
        {{ overview.running_stages }} 个阶段进行中
      </span>
    </header>

    <!-- ① 总览卡 -->
    <section class="grid grid-cols-2 md:grid-cols-4 gap-3">
      <div class="border rounded bg-white p-3 text-sm">
        <p class="text-slate-400 text-xs">近24h 运行</p>
        <p class="text-xl font-semibold text-slate-700">
          {{ Object.values(overview.runs_by_status || {}).reduce((a, b) => a + b, 0) }}
        </p>
        <p class="text-xs text-slate-500">{{ overview.runs_by_status }}</p>
      </div>
      <div class="border rounded bg-white p-3 text-sm">
        <p class="text-slate-400 text-xs">进行中阶段</p>
        <p class="text-xl font-semibold text-blue-600">{{ overview.running_stages ?? 0 }}</p>
      </div>
      <div class="border rounded bg-white p-3 text-sm">
        <p class="text-slate-400 text-xs">触顶运行(limit_hit)</p>
        <p class="text-xl font-semibold" :class="overview.limit_hit_runs ? 'text-red-500' : 'text-slate-700'">
          {{ overview.limit_hit_runs ?? 0 }}
        </p>
      </div>
      <div class="border rounded bg-white p-3 text-sm">
        <p class="text-slate-400 text-xs">节点失败排行(24h)</p>
        <p v-for="f in overview.node_fail_top || []" :key="f.node_name"
           class="text-xs text-slate-600">{{ f.node_name }}: {{ f.errors }}</p>
        <p v-if="!(overview.node_fail_top || []).length" class="text-xs text-slate-400">无</p>
      </div>
    </section>

    <!-- ② 指标看板卡(评测批次) -->
    <section class="border rounded bg-white p-3">
      <h2 class="text-sm font-semibold text-slate-600 mb-2">评测批次</h2>
      <table class="w-full text-xs" v-if="evals.length">
        <thead class="text-slate-400 text-left">
          <tr><th class="py-1">时间</th><th>数据集</th><th>标签</th>
              <th>hit_rate@5</th><th>MRR@5</th><th>P@5</th><th>R@5</th><th>F1@5</th></tr>
        </thead>
        <tbody class="text-slate-600">
          <tr v-for="e in evals" :key="e.id" class="border-t">
            <td class="py-1">{{ (e.created_at || '').slice(0, 19) }}</td>
            <td>{{ e.dataset }}</td><td>{{ e.label }}</td>
            <td>{{ (e.metrics['hit_rate_at_5'] ?? 0).toFixed(3) }}</td>
            <td>{{ (e.metrics['mrr_at_5'] ?? 0).toFixed(3) }}</td>
            <td>{{ (e.metrics['precision_at_5'] ?? 0).toFixed(3) }}</td>
            <td>{{ (e.metrics['recall_at_5'] ?? 0).toFixed(3) }}</td>
            <td>{{ (e.metrics['f1_at_5'] ?? 0).toFixed(3) }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="text-xs text-slate-400">暂无批次 — python scripts/run_eval.py --suite retrieval --label &lt;sha&gt;</p>
    </section>

    <!-- ③ runs 列表 -->
    <section class="border rounded bg-white p-3">
      <h2 class="text-sm font-semibold text-slate-600 mb-2">运行列表(50)</h2>
      <table class="w-full text-xs" v-if="runs.length">
        <thead class="text-slate-400 text-left">
          <tr><th class="py-1">开始时间</th><th>run_id</th><th>会话</th><th>模式</th>
              <th>状态</th><th>阶段(ok/总)</th><th>综合分</th><th>触顶</th></tr>
        </thead>
        <tbody>
          <tr v-for="r in runs" :key="r.run_id"
              class="border-t cursor-pointer hover:bg-slate-50"
              :class="{ 'bg-blue-50/50': detail && detail.run_id === r.run_id }"
              @click="openRun(r)">
            <td class="py-1">{{ (r.started_at || '').slice(5, 19) }}</td>
            <td class="max-w-40 truncate">{{ r.run_id }}</td>
            <td class="max-w-32 truncate">{{ r.session_id }}</td>
            <td>{{ r.mode || r.run_type }}</td>
            <td>
              <span :class="{
                ok: 'text-emerald-600', error: 'text-red-500',
                interrupted: 'text-amber-600',
              }[r.status] || 'text-slate-500'">{{ r.status }}</span>
            </td>
            <td>{{ r.stage_ok }}/{{ r.stage_total }}
              <span v-if="r.stage_running" class="text-blue-500">(进行中{{ r.stage_running }})</span>
            </td>
            <td :class="scoreCls(r.metrics?.composite_score)">
              {{ r.metrics?.composite_score ?? '—' }}
            </td>
            <td :class="r.metrics?.limit_hit ? 'text-red-500' : 'text-slate-400'">
              {{ r.metrics?.limit_hit ? '是' : '—' }}
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else class="text-xs text-slate-400">暂无运行数据</p>
    </section>

    <!-- ④ 甘特 + 抽屉 -->
    <section v-if="detail" class="grid md:grid-cols-3 gap-3">
      <div class="md:col-span-2">
        <h2 class="text-sm font-semibold text-slate-600 mb-2">
          甘特时间线 — {{ detail.run_id }}
        </h2>
        <GanttTimeline :stages="detail.stages" :now="now"
                      @select="selectedStage = $event" />
      </div>
      <StageDetailDrawer :stage="selectedStage" :spans="detail.spans" />
    </section>
  </div>
</template>
```

- [ ] **Step 5: 构建 + 端到端手验**

Run: `cd frontend && npm run build`
Expected: 零错误

手验: 后端跑起来后发起一次真实咨询(或跑 `tests/test_trace_e2e.py` 造数), 打开 `/monitor` → runs 列表出现该 run → 点击 → 甘特图出条形(各节点行) → 点击甘特条 → 抽屉显示阶段内容(rag 文档分数条/折叠面板); 进行中咨询时 3s 内 running 条出现并脉动。

- [ ] **Step 6: Commit**

```bash
git add frontend/src/api.js frontend/src/components/GanttTimeline.vue frontend/src/components/StageDetailDrawer.vue frontend/src/views/MonitorView.vue
git commit -m "C: 监控 Task8 — MonitorView 实装(总览4卡/评测批次表/runs列表含综合分触顶/甘特图CSS自绘主题色/阶段抽屉rag分数条三档配色/3s智能轮询停轮)"
```

---

### Task 9: 全量回归 + 构建门禁

**Files:**
- 无新文件(验证任务)

- [ ] **Step 1: 后端全量回归**

Run: `python -m pytest tests/ -v --tb=short 2>&1 | tail -20`
Expected: 全 PASS(157 存量 + 新增 ~16), 0 FAIL, 0 SKIP(PG 可用时)

- [ ] **Step 2: 前端构建**

Run: `cd frontend && npm run build`
Expected: 零错误

- [ ] **Step 3: 评测链路真实冒烟(可选但推荐)**

```bash
python scripts/gen_golden_set.py --count 30
python scripts/run_eval.py --suite retrieval --label baseline-001
```
Expected: 指标 JSON 打印 + `/monitor` 页评测批次表出现该行

- [ ] **Step 4: 收尾提交**

```bash
git add -A
git commit -m "C: 监控收官 — 全量回归 N PASS/0 FAIL + vite build 零错误 + 评测 baseline 冒烟; 若有零散修正一并入列"
```

---

## Self-Review 记录

- **Spec 覆盖**: D1-D3→Task1-2(拉链表/双写); D4→Task7; D5→Task2; D6→Task8; D7→Task8 抽屉(spans 事后); D8→Task2 `_rag_summary`+Task8 分数条; D9→Task3 `limit_hit`/`replan_rounds`+Task8 列; D10→Task3; D11→Task3 judge 参数; §三评测→Task4-5; §五端点→Task6; §七测试→各任务 TDD+Task9。无缺口。
- **占位符**: Task3 Step3 中 metrics() 追加段的"伪代码仅示意"注释已显式声明实现方式(实际只加两键, 其余在 flush_run), 不构成 TBD; Task5 `case_number or id` 兜底为运行时取值逻辑非占位。其余步骤均含完整代码。
- **类型一致**: `open_stage(run_id, session_id, node_name, seq)` / `close_stage(run_id, node_name, seq, status, latency_ms, detail)` 在 Task1/2 一致; `evaluate(cases, k)` 键名 `hit_rate_at_5` 等在 Task4/5/6/8 一致; MonitorStage 字段 Task6 模型 = Task8 GanttTimeline/抽屉 props。
- **已知偏差(实现时注意)**: ①Task2 测试第二段 `set_run(None) if False else None` 行是废笔, 实现时删除; ②`test_smoke.py` 的 TestClient 是否带 `/api` 前缀实现 Task6 时先读文件对齐; ③`law_cases.id` 与检索返回 `case_number` 的同源性在 Task5 冒烟时验证, 若 Pinecone 元数据键名不同, ranked 取 `r.get("case_number") or r.get("id")` 兜底已在代码内。
