"""
数据库访问层 — PostgreSQL (async psycopg3) + pgvector

职责:
    - 全局 AsyncConnectionPool 懒加载单例（API lifespan 中创建/关闭）
    - 业务表幂等 DDL: law_vector / law_cases / sessions / audit / feedback
    - 审计与反馈写入助手: record_audit / upsert_session / record_feedback

连接配置优先级: DATABASE_URL > DB_* 分项环境变量。
所有连接自动注册 pgvector 扩展（CREATE EXTENSION IF NOT EXISTS vector）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from lawApp_LangGraph.config import settings

# psycopg_async 需要 SelectorEventLoop; Windows 默认 Proactor 会让连接池全部失败
# → 模块导入时切换 policy(仅对"导入后才创建循环"的入口生效; uvicorn 需配
#    --loop lawApp_LangGraph.FastAPI.loop:selector_loop_factory)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

logger = logging.getLogger("lawApp.db")

_pool: Optional[AsyncConnectionPool] = None
# M14: 池初始化竞态防护 —— 并发首调 get_pool 时防止两个协程各建一个池
# 拉链修复: asyncio.Lock 会绑死首个 await 它的 loop, 测试/评测脚本的多
# asyncio.run 场景下后续 loop 全部抛 "bound to a different event loop"
# → 检测到 loop 更换时重建锁并废弃旧池(旧 loop 已死, 池不可复用)
_pool_lock: asyncio.Lock = asyncio.Lock()
_pool_lock_loop: Optional[asyncio.AbstractEventLoop] = None


def _rebind_lock_if_loop_changed() -> None:
    global _pool_lock, _pool_lock_loop, _pool
    loop = asyncio.get_running_loop()
    if _pool_lock_loop is not loop:
        if _pool is not None:
            logger.debug("事件循环更换, 废弃旧循环上的连接池引用")
        _pool_lock = asyncio.Lock()
        _pool_lock_loop = loop
        _pool = None


def build_dsn() -> str:
    """从配置构建 PostgreSQL 连接串(DATABASE_URL 优先, 回退 DB_* 分项)."""
    url = settings.database_url
    if url:
        return url
    return "postgresql://{user}:{password}@{host}:{port}/{name}".format(
        user=settings.db_user,
        password=settings.db_password,
        host=settings.db_host,
        port=settings.db_port,
        name=settings.db_name,
    )


async def get_pool() -> AsyncConnectionPool:
    """获取全局连接池（懒加载, M14: 全程加锁防双建; 失败不缓存坏池）。"""
    global _pool
    _rebind_lock_if_loop_changed()
    async with _pool_lock:
        if _pool is None or _pool.closed:
            # timeout=5: 池操作(取连接)挂死时快速失败, 对齐 runtime.py
            pool = AsyncConnectionPool(
                conninfo=build_dsn(),
                min_size=1,
                max_size=settings.db_pool_max,
                timeout=5,
                open=False,
            )
            try:
                await pool.open(wait=False)
                # 注册 pgvector 扩展并确保业务表存在
                async with pool.connection() as conn:
                    await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
                    await ensure_tables(conn)
            except Exception:
                # M14: DDL/setup 失败 → 不缓存坏池; 关掉刚建的池再抛,
                # 下次调用重新走 DDL
                _pool = None
                try:
                    await pool.close()
                except Exception:  # pragma: no cover — 关池失败不掩盖原异常
                    pass
                raise
            _pool = pool
            logger.info(
                "PostgreSQL 连接池就绪 | dsn=%s", build_dsn().rsplit("@", 1)[-1]
            )
        return _pool


async def close_pool() -> None:
    """关闭连接池（API 关闭时调用; M14: 先摘全局引用再关, 防关闭期间新调用）。"""
    global _pool
    pool = _pool
    _pool = None
    if pool is not None and not pool.closed:
        await pool.close()
        logger.info("PostgreSQL 连接池已关闭")


async def ensure_tables(conn: AsyncConnection) -> None:
    """幂等创建业务表（不含 checkpointer/store 的表，那些由各自 setup() 负责）。"""
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS law_vector (
            id          SERIAL PRIMARY KEY,
            law_title   TEXT NOT NULL,
            chapter     TEXT,
            article_number TEXT NOT NULL,
            content     TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT '现行有效',
            embedding   VECTOR(1024)
        );
        CREATE TABLE IF NOT EXISTS law_cases (
            id          TEXT PRIMARY KEY,
            year        TEXT,
            case_number TEXT,
            case_cause  TEXT,
            chunk_index INT,
            chunk_text  TEXT,
            embedding   VECTOR(1024)
        );
        CREATE INDEX IF NOT EXISTS law_cases_embedding_idx
            ON law_cases USING hnsw (embedding vector_cosine_ops);
        CREATE TABLE IF NOT EXISTS sessions (
            session_id  TEXT PRIMARY KEY,
            user_id     TEXT,
            meta        JSONB DEFAULT '{}'::jsonb,
            created_at  TIMESTAMPTZ DEFAULT NOW(),
            last_active_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS audit (
            id          BIGSERIAL PRIMARY KEY,
            session_id  TEXT,
            event_type  TEXT NOT NULL,
            payload     JSONB DEFAULT '{}'::jsonb,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS audit_session_idx ON audit (session_id);
        CREATE TABLE IF NOT EXISTS feedback (
            id          BIGSERIAL PRIMARY KEY,
            session_id  TEXT,
            rating      INT,
            comment     TEXT,
            answer_snapshot TEXT,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        );
        -- P1 观测层(规格 docs/superpowers/specs/2026-09-20-eval-monitoring-spec.md 决策 2):
        -- run→span 两表, Langfuse 兼容子集; 全文 JSONB 不截断
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
        CREATE INDEX IF NOT EXISTS idx_trace_spans_run ON trace_spans (run_id);
        CREATE INDEX IF NOT EXISTS idx_trace_runs_session ON trace_runs (session_id, started_at);
        -- 方案c: JSON 会话历史事件流表(写入见 dialogue_log.py, 聚合读取
        -- GET /sessions/{sid}/dialogue) —— append-only 事件, 会话级文档由聚合拼装
        CREATE TABLE IF NOT EXISTS session_dialogue_events (
            session_id  TEXT        NOT NULL,
            seq         INT         NOT NULL,
            event_type  TEXT        NOT NULL,
            payload     JSONB       DEFAULT '{}'::jsonb,
            created_at  TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (session_id, seq)
        );
        CREATE INDEX IF NOT EXISTS idx_dialogue_created
            ON session_dialogue_events (created_at);
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
        """
    )
    await conn.commit()


#  审计 / 会话 / 反馈写入助手


async def record_audit(
    session_id: str, event_type: str, payload: dict[str, Any]
) -> None:
    """写入审计事件（引用来源 / HITL 事件等）。失败仅告警，不影响主流程。"""
    try:
        pool = await get_pool()
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO audit (session_id, event_type, payload) VALUES (%s, %s, %s)",
                (
                    session_id,
                    event_type,
                    json.dumps(payload, ensure_ascii=False, default=str),
                ),
            )
            await conn.commit()
    except Exception as e:  # pragma: no cover — 审计失败不阻塞主流程
        logger.warning("audit 写入失败: %s", e)


async def upsert_session(
    session_id: str, user_id: Optional[str] = None, meta: Optional[dict] = None
) -> None:
    """新建或刷新会话活跃时间。"""
    try:
        pool = await get_pool()
        async with pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO sessions (session_id, user_id, meta, last_active_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (session_id)
                DO UPDATE SET last_active_at = NOW(),
                              user_id = COALESCE(EXCLUDED.user_id, sessions.user_id),
                              meta = sessions.meta || EXCLUDED.meta
                """,
                (
                    session_id,
                    user_id,
                    json.dumps(meta or {}, ensure_ascii=False),
                    datetime.now(timezone.utc),
                ),
            )
            await conn.commit()
    except Exception as e:  # pragma: no cover
        logger.warning("session 写入失败: %s", e)


async def record_feedback(
    session_id: str, rating: int, comment: str = "", answer_snapshot: str = ""
) -> None:
    """记录用户对回答的反馈。"""
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO feedback (session_id, rating, comment, answer_snapshot) VALUES (%s, %s, %s, %s)",
            (session_id, rating, comment, answer_snapshot[:4000]),
        )
        await conn.commit()


#  P1 观测层落库助手(trace_runs / trace_spans, 规格 2026-09-20-eval-monitoring-spec.md)


def _json_default(o: Any) -> Any:
    """观测层解包: pydantic 模型 → model_dump dict(监控页 JsonTree 分层
    渲染的前提, 字符串化会把整条变 repr); 其余未知类型 str 兜住不抛。"""
    from pydantic import BaseModel

    if isinstance(o, BaseModel):
        return o.model_dump(exclude_none=True)
    return str(o)


def _trace_json(value: Any) -> "Json":
    """JSONB 包装: 非 JSON 原生类型经 _json_default 解包/兜底, 全文不截断。"""
    from psycopg.types.json import Json

    return Json(value, dumps=lambda o: json.dumps(o, default=_json_default, ensure_ascii=False))


async def insert_trace_run(
    run_id: str,
    session_id: str,
    run_type: str,
    mode: str,
    status: str,
    query: str,
    final_answer: str,
    metrics: dict,
    started_at: float,
    ended_at: float,
) -> None:
    """写入/收尾更新一次运行(run_id 冲突时更新收尾字段)。"""
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
            (
                run_id,
                session_id,
                run_type,
                mode,
                status,
                query,
                final_answer,
                _trace_json(metrics),
                # tz=utc: fromtimestamp 默认取机器本地时区得 naive datetime,
                # 插 TIMESTAMPTZ 被 PG 当 UTC → 整体 +8h 偏移
                datetime.fromtimestamp(started_at, tz=timezone.utc),
                datetime.fromtimestamp(ended_at, tz=timezone.utc),
            ),
        )
        await conn.commit()


async def insert_trace_spans(rows: list[dict]) -> None:
    """批量写入 span 行。rows 每项键:
    run_id / span_type / name / status / input / output / state /
    latency_ms / token_usage / started_at(epoch float)。
    """
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
                [
                    (
                        r["run_id"],
                        r["span_type"],
                        r["name"],
                        r["status"],
                        _trace_json(r["input"]),
                        _trace_json(r["output"]),
                        _trace_json(r["state"]),
                        r["latency_ms"],
                        _trace_json(r["token_usage"]),
                        datetime.fromtimestamp(r["started_at"], tz=timezone.utc),
                    )
                    for r in rows
                ],
            )
        await conn.commit()


#  阶段拉链表 / 评测批次写入助手(观测旁路: 失败仅告警, 不阻塞业务)


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
    run_id: str,
    node_name: str,
    seq: int,
    status: str,
    latency_ms: int,
    detail: Optional[dict] = None,
) -> None:
    """拉链闭行: 按 (run_id, node_name, seq) 定位。开行 INSERT 与闭行
    UPDATE 是并发 fire 任务, 闭行可能先到(0 行)——有界重试等开行落地;
    重试耗尽仍 0 行则放弃(开行极端乱序/失败, 观测旁路不算错误)。"""
    try:
        pool = await get_pool()
        for _ in range(6):
            async with pool.connection() as conn:
                cur = await conn.execute(
                    "UPDATE stage_chain SET status=%s, ended_at=NOW(), "
                    "is_current=FALSE, latency_ms=%s, detail=%s "
                    "WHERE run_id=%s AND node_name=%s AND seq=%s",
                    (
                        status,
                        latency_ms,
                        json.dumps(detail or {}, ensure_ascii=False, default=str),
                        run_id,
                        node_name,
                        seq,
                    ),
                )
                await conn.commit()
                if cur.rowcount:
                    return
            await asyncio.sleep(0.05)
        logger.warning(
            "stage 闭行无匹配行(开行未落地, 观测旁路): %s/%s#%s",
            run_id, node_name, seq)
    except Exception as e:  # pragma: no cover — 观测旁路
        logger.warning("stage 闭行失败(观测旁路): %s", e)


async def insert_eval_run(dataset: str, label: str, metrics: dict, cases: list) -> None:
    """评测批次落库(P2)。"""
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO eval_runs (dataset, label, metrics, cases) "
            "VALUES (%s, %s, %s, %s)",
            (
                dataset,
                label,
                _trace_json(metrics),
                _trace_json(cases),
            ),
        )
        await conn.commit()
