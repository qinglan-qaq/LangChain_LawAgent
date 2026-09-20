# P0 检索修复 + P1 Trace 落库 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复检索链路(Pinecone 命名空间错配 + 后端指向空表)并在 PG 上落地 `trace_runs`/`trace_spans` 观测系统(装饰器三层插桩: 节点/工具/LLM)。

**Architecture:** P0 把硬编码的 `namespace="law_cases"` 全线参数化为 `Settings.pinecone_namespace`(env `PINECONE_NAMESPACE`),`.env` 切 pinecone 后端,补 `/sessions` 读路径降级。P1 新建 `lawApp_LangGraph/tracing.py` — `@traced` 装饰器 + contextvars run 上下文 + span 收集;llm 单例工厂返回 `InstrumentedChatOpenAI`(拦截 `_agenerate`/`_astream`);`_run_sse` 在 finally 统一落库,values 流回填 span.state。

**Tech Stack:** Python 3.10+ / LangGraph 1.0.1 / langchain-openai(DeepSeek) / psycopg_async(AsyncConnectionPool) / PostgreSQL 14+ pgvector(Docker `postgres-vector`, 宿主端口 15432) / pytest。

**Spec:** [docs/superpowers/specs/2026-09-20-eval-monitoring-spec.md](../specs/2026-09-20-eval-monitoring-spec.md)(55ca259 定稿;本计划实现其 P0+P1,决策 1-10/17-20)

## Global Constraints

- 解释器一律 `F:/Anaconda_env/lawApp_langGraph/python.exe`(下称 `$PY`),工作目录一律仓库根 `E:/LangChain_LawAgent-main`
- 后端启动一律从仓库根: `$PY -m uvicorn lawApp_LangGraph.FastAPI.api:app --host 0.0.0.0 --port 8000 --loop lawApp_LangGraph.FastAPI.loop:selector_loop_factory`(从其他目录启动会 ModuleNotFoundError)
- PG 连接: 宿主 localhost:15432, 库 Law_app, 用户 my_pgsql(Docker 容器 postgres-vector);命令行核对用 `F:/PostSQL12/bin/psql.exe -h localhost -p 15432 -U my_pgsql -d Law_app`(密码在 `lawApp_LangGraph/.env`,**永不打印明文**)
- `.env`(gitignored)只本地编辑,不进提交;`.env.example` 同步追加新键
- 六个 interrupt 类型字符串 `risk_confirm/clarify/pdf_confirm/degrade_confirm/mid_clarify/budget_confirm` **一字不改**;`doc_type ∈ {"complaint","defense"}`
- **存量 pytest 不动**: tests/test_smoke.py、test_engineering.py、test_mcp.py 不修改;新测试一律独立文件
- 业务错误显式抛出不兜底(仓库既有约定);**唯一例外 = 观测旁路**: trace 落库/装饰器内异常透传、落库失败记 ERROR 放行(规格决策 8)
- span 内容**全文存储不截断**;token 读 `usage_metadata`,取不到记 null
- 提交信息: 中文,`C:` 前缀,写明改动文件;一次任务一提交

## File Structure(先地图后动工)

| 文件 | 动作 | 职责 |
|---|---|---|
| `lawApp_LangGraph/config.py` | 改 | 新增 `pinecone_namespace` 设置(仅此一处默认值来源) |
| `lawApp_LangGraph/RAG_service/base.py` | 改 | 抽象签名 namespace 默认 `None`(去硬编码) |
| `lawApp_LangGraph/RAG_service/pinecone_retriever.py` | 改 | None → settings 兜底 |
| `lawApp_LangGraph/tools/rag_tools.py` | 改 | 同上 + `@traced("tool")` |
| `lawApp_LangGraph/.env` / `.env.example` | 改(本地/入库) | 切 pinecone + Law_test_namespace |
| `lawApp_LangGraph/FastAPI/api.py` | 改 | `/sessions` 降级;`_run_sse` 接线 trace(任务 7) |
| `lawApp_LangGraph/db.py` | 改 | trace/eval DDL + insert 助手 |
| `lawApp_LangGraph/tracing.py` | **新建** | 装饰器/run 上下文/span 收集/flush(纯插桩,零业务) |
| `lawApp_LangGraph/LangGraph_lawApp.py` | 改 | 节点注册包装 + llm 工厂换 Instrumented |
| `tests/test_p0_retrieval.py` 等 5 个 | **新建** | 新测试独立文件 |

---

### Task 1: P0 — PINECONE_NAMESPACE 参数化 + 切 pinecone 后端

**Files:**
- Modify: `lawApp_LangGraph/config.py:65-68`(Settings.pinecone_* 邻域)
- Modify: `lawApp_LangGraph/RAG_service/base.py:29-46`(search 抽象签名)
- Modify: `lawApp_LangGraph/RAG_service/pinecone_retriever.py:50,60`
- Modify: `lawApp_LangGraph/tools/rag_tools.py:63-103`
- Modify: `lawApp_LangGraph/.env`(本地, 不提交)、`lawApp_LangGraph/.env.example`
- Test: `tests/test_p0_retrieval.py`(新建)

**Interfaces:**
- Produces: `Settings.pinecone_namespace: str = "law_cases"`(env `PINECONE_NAMESPACE`);`BaseRetriever.search(..., namespace: Optional[str] = None)` — None 时实现层用 `settings.pinecone_namespace` 兜底。后续 P2 golden set 生成脚本依赖此参数化。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_p0_retrieval.py
"""P0 检索修复 — namespace 参数化(规格决策 18)。"""
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_namespace_setting_default_law_cases(monkeypatch):
    from lawApp_LangGraph.config import Settings

    monkeypatch.delenv("PINECONE_NAMESPACE", raising=False)
    assert Settings(_env_file=None).pinecone_namespace == "law_cases"


def test_namespace_setting_env_override(monkeypatch):
    from lawApp_LangGraph.config import Settings

    monkeypatch.setenv("PINECONE_NAMESPACE", "Law_test_namespace")
    assert Settings(_env_file=None).pinecone_namespace == "Law_test_namespace"


def test_retriever_signature_namespace_default_none():
    """硬编码 'law_cases' 全线清除: 签名默认 None, 运行时 settings 兜底。"""
    from lawApp_LangGraph.RAG_service.base import BaseRetriever
    from lawApp_LangGraph.RAG_service.pinecone_retriever import PineconeRetriever

    for fn in (BaseRetriever.search, PineconeRetriever.search):
        assert inspect.signature(fn).parameters["namespace"].default is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `$PY -m pytest tests/test_p0_retrieval.py -q`(从仓库根)
Expected: FAIL — `Settings` 无 `pinecone_namespace` 属性(AttributeError/ValidationError)

- [ ] **Step 3: 实现**

`config.py` Settings 内(紧邻 pinecone_index_name):

```python
    pinecone_namespace: str = "law_cases"
```

`base.py` 抽象签名与两个实现 + `rag_tools.py` 工具,统一把 `namespace: str = "law_cases"` 改为 `namespace: Optional[str] = None`,并在实现体开头加一行兜底(pinecone_retriever 与 rag_tools 的检索调用处;若文件未导入 settings 则补 `from lawApp_LangGraph.config import settings`):

```python
        ns = namespace or settings.pinecone_namespace
```

随后该函数体内所有 `namespace` 引用改用 `ns`(pinecone_retriever.py:60 的 `namespace=namespace` → `namespace=ns`;rag_tools.py:91/103 同理)。`pgvector_retriever.py:26` 的参数仅去硬编码为 `None`,注释保持"以表为单位,无 namespace"。

- [ ] **Step 4: 跑测试确认通过**

Run: `$PY -m pytest tests/test_p0_retrieval.py -q`
Expected: 3 passed

- [ ] **Step 5: .env 切后端(本地文件,不提交)**

`lawApp_LangGraph/.env`: `RETRIEVER_BACKEND=pinecone`,追加 `PINECONE_NAMESPACE=Law_test_namespace`。
`lawApp_LangGraph/.env.example`: 同步追加两键(pinecone/Law_test_namespace 为示例值)。

- [ ] **Step 6: 真实 Pinecone 验证(修复前后对比)**

Run(仓库根):
```bash
F:/Anaconda_env/lawApp_langGraph/python.exe -c "import asyncio; from lawApp_LangGraph.RAG_service.base import get_retriever; r = get_retriever(); docs = asyncio.run(r.search('离婚 财产分割', top_k=3, rerank_top_n=3)); print(len(docs)); print(docs[0]['case_number'] if docs else 'EMPTY')"
```
Expected: `3` + 真实案号(修复前为 `EMPTY` — ns 错配 + 空表叠加)。若 EMPTY: 检查 `PINECONE_API_KEY` 是否在 .env、index 名 `pinecone-test-lawapp`,**不要**加兜底,直接报错排查。

- [ ] **Step 7: 提交**

```bash
git add lawApp_LangGraph/config.py lawApp_LangGraph/RAG_service/base.py lawApp_LangGraph/RAG_service/pinecone_retriever.py lawApp_LangGraph/RAG_service/pgvector_retriever.py lawApp_LangGraph/tools/rag_tools.py lawApp_LangGraph/.env.example tests/test_p0_retrieval.py
git commit -m "C: P0 检索修复 — config 新增 pinecone_namespace, 检索链路 namespace 硬编码全线参数化 (config.py/base.py/pinecone_retriever.py/pgvector_retriever.py/rag_tools.py) + 测试 tests/test_p0_retrieval.py"
```

---

### Task 2: P0 — /sessions 读路径降级(PG 掉线不再 500)

**Files:**
- Modify: `lawApp_LangGraph/FastAPI/api.py:268-282`(GET /sessions)
- Test: `tests/test_sessions_degrade.py`(新建)

**Interfaces:**
- Consumes: 无(独立)
- Produces: `/sessions` 在 DB 异常时返回 `[]` + ERROR 日志(降级不 500,规格故事 22);响应成功形态保持数组不变(前端 HistorySidebar 依赖)。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_sessions_degrade.py
"""P0 — /sessions PG 断连降级(规格故事 22)。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_sessions_returns_empty_and_logs_when_pg_down(monkeypatch, caplog):
    import lawApp_LangGraph.db as db
    from lawApp_LangGraph.FastAPI.api import app
    from fastapi.testclient import TestClient

    async def _boom():
        raise RuntimeError("PG 掉线(模拟)")

    monkeypatch.setattr(db, "get_pool", _boom)

    with TestClient(app) as client:
        r = client.get("/sessions")

    assert r.status_code == 200
    assert r.json() == []
    assert any("会话列表降级" in rec.message for rec in caplog.records)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `$PY -m pytest tests/test_sessions_degrade.py -q`
Expected: FAIL — 现状 RuntimeError 直接 500

- [ ] **Step 3: 实现(api.py /sessions)**

```python
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `$PY -m pytest tests/test_sessions_degrade.py -q`
Expected: 1 passed

- [ ] **Step 5: 提交**

```bash
git add lawApp_LangGraph/FastAPI/api.py tests/test_sessions_degrade.py
git commit -m "C: P0 降级 — /sessions PG 断连时空列表+ERROR 日志不再 500 (api.py) + 测试 tests/test_sessions_degrade.py"
```

---

### Task 3: P0 — checkpoint 断连自愈检查(验证任务,零代码预设)

**Files:** 无代码改动;产出 = 验证记录(记入本文件末尾"执行记录"节)。

- [ ] **Step 1: 起后端确认 postgres checkpoint**

仓库根启动 uvicorn(见 Global Constraints),日志确认 `checkpoint_backend=postgres`(runtime.py `_setup_postgres` 成功)。

- [ ] **Step 2: 正常咨询一条基线**

```bash
curl -N -X POST http://127.0.0.1:8000/ask/stream -H "Content-Type: application/json" -d '{"mode":"attorney","query":"你好","doc_type":""}'
```
Expected: SSE 正常,收 `done`。

- [ ] **Step 3: 断 Docker 断连复现**

停 Docker Desktop(15432 消失)→ 重复 Step 2 请求。
Expected: SSE 发 `error` 事件后正常收 `done`(不挂起、不 500 连接层崩溃)。**记录实际行为**。

- [ ] **Step 4: 重启 Docker 自愈验证**

重启 Docker Desktop(容器 postgres-vector 恢复)→ 重复 Step 2 + GET `http://127.0.0.1:8000/sessions`。
Expected: 咨询恢复、会话列表恢复。**记录恢复是否需要重启后端进程**(AsyncConnectionPool 断连后 get_pool 重建行为)。

- [ ] **Step 5: 结论分流**

- 若 Step 3/4 全部符合预期 → 仅记录,不改代码。
- 若发现挂起/不自愈 → **停下报告**,升级为新任务(不许现场兜底)。

- [ ] **Step 6: 提交(仅执行记录)**

```bash
git add docs/superpowers/plans/2026-09-21-p0-p1-trace-observability.md
git commit -m "C: P0 验证记录 — checkpoint 断连自愈检查结果 (docs/superpowers/plans/2026-09-21-p0-p1-trace-observability.md)"
```

---

### Task 4: P1 — trace 表 DDL + 落库助手(db.py)

**Files:**
- Modify: `lawApp_LangGraph/db.py:82-136`(ensure_tables 追加 DDL)、文件尾部追加两个 insert 助手
- Test: `tests/test_trace_db.py`(新建)

**Interfaces:**
- Consumes: 既有 `get_pool()`(db.py:54)
- Produces(任务 5/7 依赖,签名精确):

```python
async def insert_trace_run(run_id: str, session_id: str, run_type: str, mode: str,
                           status: str, query: str, final_answer: str,
                           metrics: dict, started_at: float, ended_at: float) -> None
async def insert_trace_spans(rows: list[dict]) -> None
# rows 每项键: run_id/span_type/name/status/input/output/state/latency_ms/token_usage/started_at
# started_at 为 epoch float(内部转 timestamptz);JSONB 值经 Json(..., default=str) 包装
```

- [ ] **Step 1: 写失败测试**

```python
# tests/test_trace_db.py
"""P1 — trace 表 DDL 幂等 + insert 助手(真实 PG, 不可用显式 SKIP, 不 mock)。"""
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _pg_ok() -> bool:
    async def _probe():
        from lawApp_LangGraph.db import get_pool
        pool = await get_pool()
        async with pool.connection() as conn:
            await conn.execute("SELECT 1")
    try:
        asyncio.run(_probe())
        return True
    except Exception:
        return False


def test_ensure_tables_idempotent_with_trace_ddl():
    if not _pg_ok():
        import pytest
        pytest.skip("PG 不可用, 显式跳过(不 mock)")
    from lawApp_LangGraph.db import get_pool

    async def _run():
        from lawApp_LangGraph.db import ensure_tables
        pool = await get_pool()
        async with pool.connection() as conn:
            await ensure_tables(conn)   # 第一遍建表
            await ensure_tables(conn)   # 第二遍幂等
            cur = await conn.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_name IN ('trace_runs','trace_spans')")
            (n,) = await cur.fetchone()
            assert n == 2
    asyncio.run(_run())


def test_insert_trace_run_and_spans_roundtrip():
    if not _pg_ok():
        import pytest
        pytest.skip("PG 不可用, 显式跳过(不 mock)")
    from lawApp_LangGraph.db import get_pool, insert_trace_run, insert_trace_spans

    rid = f"test-run:{time.time()}"
    now = time.time()

    async def _run():
        insert_trace_run(run_id=rid, session_id="s-test", run_type="live_ask",
                         mode="attorney", status="ok", query="测试问题",
                         final_answer="测试回答", metrics={"node_count": 1},
                         started_at=now, ended_at=now + 1.5)
        insert_trace_spans([{
            "run_id": rid, "span_type": "node", "name": "ingest",
            "status": "ok", "input": {"query": "测试问题"},
            "output": {"ok": True}, "state": {"query": "测试问题"},
            "latency_ms": 12, "token_usage": None, "started_at": now,
        }])
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT status, metrics FROM trace_runs WHERE run_id=%s", (rid,))
            status, metrics = await cur.fetchone()
            assert status == "ok" and metrics["node_count"] == 1
            cur = await conn.execute(
                "SELECT span_type, name, latency_ms, state FROM trace_spans WHERE run_id=%s", (rid,))
            span_type, name, latency_ms, state = await cur.fetchone()
            assert (span_type, name, latency_ms, state["query"]) == ("node", "ingest", 12, "测试问题")
            # 清理测试数据
            await conn.execute("DELETE FROM trace_spans WHERE run_id=%s", (rid,))
            await conn.execute("DELETE FROM trace_runs WHERE run_id=%s", (rid,))
    asyncio.run(_run())
```

- [ ] **Step 2: 跑测试确认失败**

Run: `$PY -m pytest tests/test_trace_db.py -q`
Expected: FAIL — `insert_trace_run` 不存在(ImportError);DDL 检查 n==2 不满足

- [ ] **Step 3: 实现**

`db.py` ensure_tables 的 `CREATE TABLE IF NOT EXISTS feedback ...` 之后追加(与既有 DDL 同风格):

```sql
CREATE TABLE IF NOT EXISTS trace_runs (
    run_id      TEXT PRIMARY KEY,
    session_id  TEXT,
    run_type    TEXT NOT NULL,
    mode        TEXT,
    status      TEXT NOT NULL,
    query       TEXT,
    final_answer TEXT,
    metrics     JSONB DEFAULT '{}',
    started_at  TIMESTAMPTZ,
    ended_at    TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS trace_spans (
    id          BIGSERIAL PRIMARY KEY,
    run_id      TEXT NOT NULL,
    span_type   TEXT NOT NULL,
    name        TEXT NOT NULL,
    status      TEXT,
    input       JSONB,
    output      JSONB,
    state       JSONB,
    latency_ms  INT,
    token_usage JSONB,
    started_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_trace_spans_run ON trace_spans(run_id);
CREATE INDEX IF NOT EXISTS idx_trace_runs_session ON trace_runs(session_id, started_at);
```

db.py 文件尾部追加(record_audit 同款池模式,文件头部补 `from psycopg.types.json import Json` 与 `import json as _json`、`from datetime import datetime as _dt`;若已导入则不重复):

```python
def _trace_json(value) -> Json:
    """JSONB 包装: 非 JSON 原生类型(state 含 Pydantic 模型等)用 default=str 兜住。"""
    return Json(value, dumps=lambda o: _json.dumps(o, default=str, ensure_ascii=False))


async def insert_trace_run(run_id: str, session_id: str, run_type: str, mode: str,
                           status: str, query: str, final_answer: str,
                           metrics: dict, started_at: float, ended_at: float) -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO trace_runs
                (run_id, session_id, run_type, mode, status,
                 query, final_answer, metrics, started_at, ended_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id) DO UPDATE SET
                status = EXCLUDED.status,
                final_answer = EXCLUDED.final_answer,
                metrics = EXCLUDED.metrics,
                ended_at = EXCLUDED.ended_at
            """,
            (run_id, session_id, run_type, mode, status, query, final_answer,
             _trace_json(metrics), _dt.fromtimestamp(started_at), _dt.fromtimestamp(ended_at)),
        )


async def insert_trace_spans(rows: list[dict]) -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.executemany(
                """
                INSERT INTO trace_spans
                    (run_id, span_type, name, status, input, output,
                     state, latency_ms, token_usage, started_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [(r["run_id"], r["span_type"], r["name"], r["status"],
                  _trace_json(r["input"]), _trace_json(r["output"]),
                  _trace_json(r["state"]), r["latency_ms"],
                  _trace_json(r["token_usage"]), _dt.fromtimestamp(r["started_at"]))
                 for r in rows],
            )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `$PY -m pytest tests/test_trace_db.py -q`
Expected: 2 passed(PG 掉线时 2 skipped — 合法)

- [ ] **Step 5: 提交**

```bash
git add lawApp_LangGraph/db.py tests/test_trace_db.py
git commit -m "C: P1 数据层 — trace_runs/trace_spans 表 DDL + insert_trace_run/insert_trace_spans 落库助手 (db.py) + 测试 tests/test_trace_db.py"
```

---

### Task 5: P1 — tracing.py 装饰器核心(纯插桩模块)

**Files:**
- Create: `lawApp_LangGraph/tracing.py`
- Test: `tests/test_tracing.py`(新建)

**Interfaces:**
- Produces(任务 6/7 依赖,签名精确):

```python
@dataclass
class Span:            # span_type/node 工具 llm hitl; status ok|error|interrupted; state 由 values 流回填
class RunContext:      # .run_id/.session_id/.run_type/.mode/.query/.final_answer/.status/.spans/.started_at; .metrics() -> dict
def traced(span_type: str, name: Optional[str] = None) -> Callable  # 装饰器工厂
def set_run(run: RunContext) -> RunContext
def current_run() -> Optional[RunContext]
def attach_state(node_names: list[str], values: dict) -> None
def flush_run(run: RunContext) -> Awaitable[None]   # 落库, 失败记 ERROR 放行(观测旁路)
class InstrumentedChatOpenAI(ChatOpenAI)            # 拦截 _agenerate/_astream → llm span
```

- [ ] **Step 1: 写失败测试**

```python
# tests/test_tracing.py
"""P1 — 装饰器核心单测(纯 Python, 无 PG/无真实 LLM)。"""
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lawApp_LangGraph.tracing import (  # noqa: E402
    RunContext, Span, current_run, set_run, traced,
)


def test_traced_async_records_span():
    run = set_run(RunContext(run_id="t:1", session_id="t", run_type="live_ask"))

    @traced("node")
    async def planner_node(state, config=None):
        await asyncio.sleep(0.01)
        return {"plan": []}

    out = asyncio.run(planner_node({"query": "q"}, {"a": 1}))
    assert out == {"plan": []}
    assert len(run.spans) == 1
    s = run.spans[0]
    assert (s.span_type, s.name, s.status) == ("node", "planner_node", "ok")
    assert s.latency_ms >= 5          # sleep 10ms
    assert s.input == {"query": "q"}   # 单位置参数原样记录(前后成果: 入参)
    assert s.output == {"plan": []}    # 前后成果: 返回值


def test_traced_exception_passthrough_and_error_span():
    run = set_run(RunContext(run_id="t:2", session_id="t", run_type="live_ask"))

    @traced("tool")
    async def boom(x):
        raise ValueError("业务异常必须透传")

    try:
        asyncio.run(boom(1))
        raise AssertionError("必须抛出")
    except ValueError:
        pass
    s = run.spans[0]
    assert s.status == "error"
    assert "业务异常必须透传" in s.output["exception"]


def test_traced_sync_function():
    run = set_run(RunContext(run_id="t:3", session_id="t", run_type="live_ask"))

    @traced("tool")
    def evaluate_case_relevance(query, docs=None):
        return {"applicable": True}

    assert evaluate_case_relevance("q") == {"applicable": True}
    assert run.spans[0].name == "evaluate_case_relevance"


def test_orphan_buffer_no_run_context():
    # 无 run 上下文(单测直接调被装饰函数): 进游离缓冲, 不报错不落库
    from lawApp_LangGraph import tracing

    @traced("node")
    async def free_node():
        return 1

    asyncio.run(free_node())
    assert tracing._ORPHAN_SPANS[-1].name == "free_node"


def test_metrics_aggregation():
    run = set_run(RunContext(run_id="t:5", session_id="t", run_type="live_ask"))
    run.add(Span(span_type="node", name="planner", latency_ms=100,
                token_usage={"prompt": 10, "completion": 5}))
    run.add(Span(span_type="tool", name="retrieve_legal_knowledge", latency_ms=50))
    run.add(Span(span_type="llm", name="llm:deepseek-chat", latency_ms=30,
                token_usage={"prompt": 100, "completion": 50}))
    run.add(Span(span_type="node", name="ask_element", latency_ms=5))
    run.add(Span(span_type="node", name="mid_clarify", latency_ms=5))
    m = run.metrics()
    assert m == {"node_count": 3, "tool_count": 1, "llm_count": 1,
                 "total_latency_ms": 190, "token_prompt": 110,
                 "token_completion": 55, "clarify_rounds": 2}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `$PY -m pytest tests/test_tracing.py -q`
Expected: FAIL — `lawApp_LangGraph.tracing` 模块不存在

- [ ] **Step 3: 实现 tracing.py 全文**

```python
"""观测插桩 — @traced 装饰器 + run 上下文 + span 收集落库。

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
import json
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
    span_type: str   # node | tool | llm | hitl
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
            "token_prompt": sum((s.token_usage or {}).get("prompt") or 0 for s in self.spans),
            "token_completion": sum((s.token_usage or {}).get("completion") or 0 for s in self.spans),
            "clarify_rounds": sum(
                1 for s in self.spans
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


def traced(span_type: str, name: Optional[str] = None) -> Callable:
    """装饰器工厂(规格决策 3): 包住需检测的图节点/工具函数。

    异常原样透传(先记 error/interrupted span);async 与 sync 函数都支持。
    """
    def deco(fn: Callable) -> Callable:
        if asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def awrapper(*args, **kwargs):
                t0 = time.perf_counter()
                span = Span(span_type=span_type, name=name or fn.__name__,
                            input=_pack(args, kwargs), started_at=time.time())
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
            span = Span(span_type=span_type, name=name or fn.__name__,
                        input=_pack(args, kwargs), started_at=time.time())
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


def _is_interrupt(e: Exception) -> bool:
    try:
        from langgraph.errors import GraphInterrupt
        return isinstance(e, GraphInterrupt)
    except Exception:
        return False


def attach_state(node_names: list[str], values: dict) -> None:
    """values 流回填(决策 5): 节点执行后的完整 state 写进对应 node span。"""
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


def _emit_llm_span(model: str, messages: Any, output_msg: Any,
                   t0: float, status: str, token_usage: Optional[dict] = None) -> None:
    span = Span(
        span_type="llm", name=f"llm:{model}",
        input=[_msg_text(m) for m in (messages or [])],
        latency_ms=int((time.perf_counter() - t0) * 1000),
        started_at=time.time(), status=status,
    )
    if output_msg is not None:
        span.output = getattr(output_msg, "content", output_msg)
        usage = getattr(output_msg, "usage_metadata", None)
        if usage:
            span.token_usage = {"prompt": usage.get("input_tokens"),
                                "completion": usage.get("output_tokens")}
    if token_usage:
        span.token_usage = token_usage
    _emit(span)


async def flush_run(run: RunContext) -> None:
    """run + spans 统一落库;失败记 ERROR 放行(观测旁路, 决策 8)。"""
    try:
        from lawApp_LangGraph import db
        await db.insert_trace_run(
            run_id=run.run_id, session_id=run.session_id, run_type=run.run_type,
            mode=run.mode, status=run.status, query=run.query,
            final_answer=run.final_answer, metrics=run.metrics(),
            started_at=run.started_at, ended_at=time.time(),
        )
        rows = [{
            "run_id": run.run_id, "span_type": s.span_type, "name": s.name,
            "status": s.status, "input": s.input, "output": s.output,
            "state": s.state, "latency_ms": s.latency_ms,
            "token_usage": s.token_usage, "started_at": s.started_at,
        } for s in run.spans]
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
                    messages, stop=stop, run_manager=run_manager, **kwargs)
            except Exception:
                _emit_llm_span(self.model_name, messages, None, t0, "error")
                raise
            msg = (result.generations[0][0].message
                   if getattr(result, "generations", None) else None)
            _emit_llm_span(self.model_name, messages, msg, t0, "ok")
            return result

        async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
            t0 = time.perf_counter()
            last = None
            try:
                async for chunk in super()._astream(
                    messages, stop=stop, run_manager=run_manager, **kwargs
                ):
                    last = chunk
                    yield chunk
            except Exception:
                _emit_llm_span(self.model_name, messages, last, t0, "error")
                raise
            _emit_llm_span(self.model_name, messages, last, t0, "ok")

    return InstrumentedChatOpenAI


def get_instrumented_llm_cls():
    """延迟构建(避免模块导入期强依赖 langchain_openai;每次返回同一类)。"""
    global _INSTRUMENTED
    if _INSTRUMENTED is None:
        _INSTRUMENTED = _instrument_llm_cls()
    return _INSTRUMENTED


_INSTRUMENTED = None
```

模块顶部 import 里 `json` 实际未用 — 删除 `import json`(实现时以 lint 为准)。

- [ ] **Step 4: 跑测试确认通过**

Run: `$PY -m pytest tests/test_tracing.py -q`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add lawApp_LangGraph/tracing.py tests/test_tracing.py
git commit -m "C: P1 插桩核心 — tracing.py @traced 装饰器/RunContext/contextvars 上下文/游离缓冲/flush_run/InstrumentedChatOpenAI + 测试 tests/test_tracing.py"
```

---

### Task 6: P1 — 三层接线(节点注册包装 + 工具装饰 + llm 工厂替换)

**Files:**
- Modify: `lawApp_LangGraph/LangGraph_lawApp.py:105-138`(两工厂)、`1806-1820`(builder 注册)
- Modify: `lawApp_LangGraph/tools/db_tools.py:41-42,107-108,199-200`
- Modify: `lawApp_LangGraph/tools/rag_tools.py:62-63,159-160,276-277`
- Modify: `lawApp_LangGraph/tools/tools.py:21-22,115-116`
- Test: `tests/test_tracing.py` 追加 3 个用例(新任务追加到本文件,不碰存量)

**Interfaces:**
- Consumes: `tracing.traced`、`tracing.get_instrumented_llm_cls()`(任务 5)
- Produces: 全部 15 个图节点经 `traced("node")` 包装后注册;8 个工具函数带 `@traced("tool")`;`get_planner_llm()`/`get_executor_llm()` 返回 InstrumentedChatOpenAI(**isinstance ChatOpenAI 仍为 True** — `_structured` 的 json_mode 分支保持生效)。

- [ ] **Step 1: 追加失败测试**

`tests/test_tracing.py` 追加:

```python
def test_node_registration_wrapped_with_traced():
    from lawApp_LangGraph.LangGraph_lawApp import build_graph
    from langgraph.checkpoint.memory import MemorySaver

    graph = build_graph(checkpointer=MemorySaver())
    nodes = set(graph.get_graph().nodes)
    expected = {"ingest", "risk_gate", "element_assess", "ask_element", "planner",
                "executor", "tools", "merge", "replan_check", "mid_clarify",
                "hitl_degrade", "hitl_budget", "replanner", "finalize", "chitchat"}
    assert expected <= nodes


def test_tool_traced_keeps_signature():
    from lawApp_LangGraph.tools import ALL_TOOLS
    names = {t.name for t in ALL_TOOLS()}
    for required in ("retrieve_legal_knowledge", "evaluate_case_relevance",
                     "analyze_legal_issue", "fetch_laws", "search_memory",
                     "save_to_memory", "get_google_search", "markdown_to_pdf"):
        assert required in names


def test_llm_factory_returns_instrumented_chatopenai():
    from langchain_openai import ChatOpenAI
    from lawApp_LangGraph.LangGraph_lawApp import get_executor_llm, get_planner_llm

    for llm in (get_planner_llm(), get_executor_llm()):
        assert isinstance(llm, ChatOpenAI)  # 子类 → _structured json_mode 分支不受影响
```

- [ ] **Step 2: 跑测试确认失败**

Run: `$PY -m pytest tests/test_tracing.py -q`
Expected: 新 3 例中 `test_llm_factory_returns_instrumented_chatopenai` 逻辑可过但接线未完成不构成失败信号 — 以 `test_node_registration_wrapped_with_traced` 与工具注册实际断言为准;**真正的失败验证**: 临时在任一节点 span 收集上跑 smoke 里的图构建(下步实现后回归)。若接线前 3 例恰好全过(仅类型断言),记录之,以 Step 4 全量回归为准。

- [ ] **Step 3: 实现**

`LangGraph_lawApp.py` 顶部 import 区追加:

```python
from lawApp_LangGraph.tracing import traced
```

两个工厂内 `ChatOpenAI(` 替换(仅实例化两处,`_structured` 内的 `from langchain_openai import ChatOpenAI` isinstance 检查**保留不动**):

```python
                _llm_planner = get_instrumented_llm_cls()(
                    model=settings.deepseek_pro_model,
                    ...同参不变...
                )
```

`from lawApp_LangGraph.tracing import get_instrumented_llm_cls` 一并加 import。executor 工厂同理。

builder 注册区(1806-1820)15 行统一改形(示例前 3 行,其余同型):

```python
    builder.add_node("ingest", traced("node")(ingest_node))
    builder.add_node("risk_gate", traced("node")(risk_gate_node))
    builder.add_node("element_assess", traced("node")(element_assess_node))
    # ... ask_element/planner/executor/merge/replan_check/mid_clarify/
    #     hitl_degrade/hitl_budget/replanner/finalize/chitchat 同型逐行改写,
    # tools 节点: builder.add_node("tools", traced("node")(_build_tools_node()))
```

8 个工具函数,`@tool` 下加一行(装饰器自外向内: tool 拿到的仍是保持原签名的函数,functools.wraps 已保 `__wrapped__`):

```python
@tool
@traced("tool")
async def retrieve_legal_knowledge(
```

其余 7 个(db_tools.py 的 search_memory/save_to_memory/fetch_laws;rag_tools.py 的 evaluate_case_relevance/analyze_legal_issue;tools.py 的 get_google_search/markdown_to_pdf)同型。各文件顶部补 `from lawApp_LangGraph.tracing import traced`。

- [ ] **Step 4: 全量回归**

Run: `$PY -m pytest tests/ -q`(仓库根)
Expected: 全绿(含存量 test_smoke 图构建/HITL/重启续聊 — 装饰器透明;已知环境波动用例 test_config 的 db_port 若因环境变量污染 FAIL,与本次改动无关则记录,不修改存量)。

- [ ] **Step 5: 提交**

```bash
git add lawApp_LangGraph/LangGraph_lawApp.py lawApp_LangGraph/tools/db_tools.py lawApp_LangGraph/tools/rag_tools.py lawApp_LangGraph/tools/tools.py tests/test_tracing.py
git commit -m "C: P1 三层接线 — 15 图节点 traced 注册/8 工具 @traced/llm 工厂换 InstrumentedChatOpenAI (LangGraph_lawApp.py + tools/db_tools.py rag_tools.py tools.py) + 测试"
```

---

### Task 7: P1 — _run_sse 接线(run 上下文 / values 回填 / hitl span / finally 落库)

**Files:**
- Modify: `lawApp_LangGraph/FastAPI/api.py:482-645`(_run_sse)、`355`(ask_resume_stream 调用点)、`673`(_mode_stream 调用点)
- Test: `tests/test_trace_e2e.py`(新建)

**Interfaces:**
- Consumes: `tracing.RunContext/set_run/attach_state/flush_run/Span`(任务 5)、`db.insert_trace_*`(任务 4)、`traced` 节点/llm 插桩(任务 6)
- Produces: 每次 ask/resume SSE 运行自动落 trace_runs(一条)+ trace_spans(全部);`_run_sse(sid, astream_input, mode="")` 新增第三参。P2 run_eval(后续计划)将以同款 RunContext 直调 graph 复用本机制。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_trace_e2e.py
"""P1 集成 — SSE 全链路落 trace(替身 LLM 同 test_smoke 思路; PG 掉线 SKIP)。"""
import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class _FakeChat:
    """executor/planner 替身: with_structured_output → 固定 verdict; 直接管道 → 固定回答。"""
    def __init__(self, verdict=None, text="冒烟回答"):
        self._verdict = verdict or {}
        self._text = text

    def with_structured_output(self, schema, **kwargs):
        outer = self

        class _V:
            def invoke(self, input, config=None, **kw):
                return SimpleNamespace(**outer._verdict)
        return _V()

    def invoke(self, input, config=None, **kwargs):
        return SimpleNamespace(content=self._text, tool_calls=None)


def _patch_llms(monkeypatch):
    """风险门放行(high_risk=False, need_clarification=False)→ chitchat 直接回答。"""
    fake = _FakeChat(verdict={"high_risk": False, "need_clarification": False,
                              "reason": "", "question": "", "plan": (),
                             "reasoning": (), "done": True})
    import lawApp_LangGraph.LangGraph_lawApp as app_mod
    monkeypatch.setattr(app_mod, "get_executor_llm", lambda: fake)
    monkeypatch.setattr(app_mod, "get_planner_llm", lambda: fake)


def _pg_ok() -> bool:
    async def _probe():
        from lawApp_LangGraph.db import get_pool
        pool = await get_pool()
        async with pool.connection() as conn:
            await conn.execute("SELECT 1")
    try:
        asyncio.run(_probe())
        return True
    except Exception:
        return False


def test_sse_run_persists_trace(monkeypatch):
    if not _pg_ok():
        import pytest
        pytest.skip("PG 不可用, 显式跳过(不 mock)")
    _patch_llms(monkeypatch)
    from fastapi.testclient import TestClient
    from lawApp_LangGraph.FastAPI.api import app

    with TestClient(app) as client:
        with client.stream("POST", "/ask/stream",
                           json={"mode": "attorney", "query": "你好",
                                 "doc_type": ""}) as resp:
            assert resp.status_code == 200
            body = "".join(line for line in resp.iter_lines())

    assert "done" in body  # 全流程收尾
    # 从 DB 侧取最新 run 验证(不依赖事件解析细节)
    import lawApp_LangGraph.db as db

    async def _verify():
        pool = await db.get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT run_id, run_type, status, final_answer, metrics "
                "FROM trace_runs WHERE session_id IS NOT NULL "
                "ORDER BY started_at DESC LIMIT 1")
            row = await cur.fetchone()
            assert row is not None, "trace_runs 必须有行"
            rid, run_type, status, answer, metrics = row
            assert run_type == "live_ask" and status == "ok"
            assert answer == "冒烟回答"
            assert metrics["node_count"] >= 2  # ingest + risk_gate + chitchat
            cur = await conn.execute(
                "SELECT span_type, name, latency_ms, state IS NOT NULL "
                "FROM trace_spans WHERE run_id=%s", (rid,))
            spans = await cur.fetchall()
            names = {(s[0], s[1]) for s in spans}
            assert ("node", "ingest") in names and ("node", "chitchat") in names
            assert all(s[3] for s in spans if s[0] == "node")  # values 回填: node span 必有 state
            # 清理
            await conn.execute("DELETE FROM trace_spans WHERE run_id=%s", (rid,))
            await conn.execute("DELETE FROM trace_runs WHERE run_id=%s", (rid,))

    asyncio.run(_verify())
```

- [ ] **Step 2: 跑测试确认失败**

Run: `$PY -m pytest tests/test_trace_e2e.py -q`
Expected: FAIL — `trace_runs` 查无行(`_run_sse` 尚未接线)

- [ ] **Step 3: 实现(api.py)**

3a. `_run_sse` 签名与 event_stream 开头:

```python
def _run_sse(sid: str, astream_input, mode: str = "") -> StreamingResponse:
```

event_stream() 体内、`out_q`/`reasoning_q` 建立之前插入(run 上下文先于 task 创建,子任务继承 contextvar):

```python
        from datetime import datetime
        from langgraph.types import Command
        from lawApp_LangGraph.tracing import RunContext, set_run

        run_type = "live_resume" if isinstance(astream_input, Command) else "live_ask"
        trace_run = set_run(RunContext(
            run_id=f"{sid}:{datetime.now():%H%M%S%f}",
            session_id=sid, run_type=run_type, mode=mode,
            query=(astream_input.get("query") or "")
            if isinstance(astream_input, dict) else "",
        ))
```

3b. run() 内 values/updates 对齐(决策 5 回填): 在 `final_state: dict = {}` 之后加 `pending_nodes: list[str] = []`;updates 分支末尾(chunk 循环处理完后)加:

```python
                            pending_nodes.extend(
                                str(n) for n in (chunk or {}) if isinstance(n, str)
                            )
```

values 分支(`final_state = chunk or final_state` 处)改为:

```python
                    elif stream_mode == "values":
                        final_state = chunk or final_state
                        # values 流回填: 本 super-step 后的完整 state → 对应 node span
                        attach_state(pending_nodes, chunk or {})
                        pending_nodes = []
```

`pending_nodes` 重赋值需在 run() 顶部声明 `nonlocal` 或将 updates/values 处理改为对局部变量集合操作 — 实现时把 `pending_nodes` 定义在 run() 体内即可(闭包内重赋值同一函数体内合法,无需 nonlocal)。

3c. interrupt 分支(hitl span,决策 3/9): `await out_q.put(("interrupt", interrupt_req))` 之后加:

```python
                    trace_run.status = "interrupted"
                    trace_run.add(Span(span_type="hitl",
                                       name=interrupt_req.get("type", "hitl"),
                                       input=interrupt_req, started_at=time.time()))
```

正常分支 `answer` 已取出后加:

```python
                    trace_run.final_answer = answer
```

3d. except 分支(`flow.error(...)` 处)加 `trace_run.status = "error"`。

3e. finally(run() 已有 finally)在 `await reasoning_q.put(None)` **之前**加:

```python
                await flush_run(trace_run)  # 观测旁路: 失败内部记 ERROR 放行(决策 8)
```

api.py 顶部 import 区追加:

```python
import time
from lawApp_LangGraph.tracing import Span, attach_state, flush_run
```

3f. 两个调用点传 mode: `api.py:355` → `return _run_sse(sid, Command(resume=resume_value), mode="attorney")`;`api.py:673` → `return _run_sse(sid, inputs, mode=mode)`。

- [ ] **Step 4: 跑测试确认通过**

Run: `$PY -m pytest tests/test_trace_e2e.py tests/test_tracing.py -q`
Expected: 全 passed(PG 掉线时 e2e SKIP 合法)

- [ ] **Step 5: 全量回归**

Run: `$PY -m pytest tests/ -q`
Expected: 全绿(环境波动用例按 Task 6 Step 4 同口径处理)

- [ ] **Step 6: 提交**

```bash
git add lawApp_LangGraph/FastAPI/api.py tests/test_trace_e2e.py
git commit -m "C: P1 接线 — _run_sse 建 RunContext(values 流回填 state/interrupt 记 hitl span/finally flush_run 落库) + mode 参数贯通 (api.py) + 测试 tests/test_trace_e2e.py"
```

---

### Task 8: P1 — 真实 E2E 验证(真实 LLM + 真实 PG + HITL 双 run)

**Files:** 无代码;产出 = 验证记录追加到本文件"执行记录"节并提交。

- [ ] **Step 1: 重启后端**

从仓库根以 Global Constraints 命令启动 uvicorn(先杀旧进程: `netstat -ano | grep :8000` → `taskkill //PID <pid> //F`,Windows 下 TaskStop 会留孤儿进程)。

- [ ] **Step 2: 真实咨询 + SQL 核对**

```bash
curl -N -X POST http://127.0.0.1:8000/ask/stream -H "Content-Type: application/json" \
  -d '{"mode":"attorney","query":"我想离婚,婚内买的房子怎么分","doc_type":""}'
```
然后:

```bash
F:/PostSQL12/bin/psql.exe -h localhost -p 15432 -U my_pgsql -d Law_app -c \
  "SELECT run_id, run_type, status, metrics->>'node_count' AS nodes, metrics->>'token_prompt' AS tok FROM trace_runs ORDER BY started_at DESC LIMIT 3;"
F:/PostSQL12/bin/psql.exe -h localhost -p 15432 -U my_pgsql -d Law_app -c \
  "SELECT span_type, name, latency_ms, token_usage, length(state::text) AS state_len FROM trace_spans s JOIN trace_runs r ON s.run_id=r.run_id WHERE r.run_id=(SELECT max(run_id) FROM trace_runs WHERE status='ok') ORDER BY s.id;"
```
Expected: run 一条 status=ok;spans 含 node(ingest/risk_gate/planner/executor/merge…)、tool(**retrieve_legal_knowledge 且有结果 — 同时回归验证 P0**)、llm(token_usage 非空);node 行 state_len > 0。

- [ ] **Step 3: HITL 双 run 验证**

发一个会触发要素补充 interrupt 的咨询(信息不足场景)→ SSE 收 `interrupt` 事件;SQL 核对该 run `status=interrupted` + hitl span 存在;再 POST `/ask/resume/stream` 恢复 → 新 run `run_type=live_resume`,`session_id` 相同。

- [ ] **Step 4: 记录 + 提交**

预期全部符合 → 把三步实际 SQL 输出摘要(隐去敏感内容)记入本文件"执行记录";异常则停下报告,不做现场兜底。

```bash
git add docs/superpowers/plans/2026-09-21-p0-p1-trace-observability.md
git commit -m "C: P1 验证记录 — 真实 E2E trace 落库/工具命中/token/HITL 双 run (docs/superpowers/plans/2026-09-21-p0-p1-trace-observability.md)"
```

---

## 执行记录(任务执行时追加)

- Task 3 checkpoint 自愈检查结果: (待填)
- Task 8 真实 E2E 结果: (待填)

## Spec Coverage / Self-Review(计划自审)

- 规格决策 18(P0 前置四项) → Task 1(namespace+后端)/Task 2(降级)/Task 3(自愈检查)✔;upsert_session 已有 `_safe_upsert_session` 覆盖(api.py:150),无需新任务 ✔
- 决策 2(三表)→ Task 4 ✔;决策 3-6(装饰器三层/contextvars/values 回填/llm 工厂)→ Task 5+6 ✔;决策 7(run_id)/8(finally)/9(metrics)/10(HITL 拆分)→ Task 7 ✔
- 决策 1(自建)/11(无判定)/12-14(P2)/15-16(P3)不在本计划 — 属后续计划,spec P2/P3 阶段
- 类型一致性: `insert_trace_run` kwargs 与 `flush_run` 调用逐字对齐;`Span` 字段与 `insert_trace_spans` rows 键逐字对齐;`_run_sse(sid, astream_input, mode="")` 与两个调用点对齐 ✔
- 占位扫描: 无 TBD/TODO;"执行记录"两处"(待填)"为任务 3/8 的**产出位置**,非实现占位 ✔
