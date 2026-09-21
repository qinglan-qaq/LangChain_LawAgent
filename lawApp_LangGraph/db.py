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


def build_dsn() -> str:
    """从配置构建 PostgreSQL 连接串(DATABASE_URL 优先, 回退 DB_* 分项)."""
    url = settings.database_url
    if url:
        return url
    return (
        "postgresql://{user}:{password}@{host}:{port}/{name}".format(
            user=settings.db_user,
            password=settings.db_password,
            host=settings.db_host,
            port=settings.db_port,
            name=settings.db_name,
        )
    )


async def get_pool() -> AsyncConnectionPool:
    """获取全局连接池（懒加载）。"""
    global _pool
    if _pool is None or _pool.closed:
        _pool = AsyncConnectionPool(
            conninfo=build_dsn(),
            min_size=1,
            max_size=settings.db_pool_max,
            open=False,
        )
        await _pool.open(wait=False)
        # 注册 pgvector 扩展并确保业务表存在
        async with _pool.connection() as conn:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await ensure_tables(conn)
        logger.info("PostgreSQL 连接池就绪 | dsn=%s", build_dsn().rsplit("@", 1)[-1])
    return _pool


async def close_pool() -> None:
    """关闭连接池（API 关闭时调用）。"""
    global _pool
    if _pool is not None and not _pool.closed:
        await _pool.close()
        logger.info("PostgreSQL 连接池已关闭")
    _pool = None


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
        """
    )
    await conn.commit()


#  审计 / 会话 / 反馈写入助手


async def record_audit(session_id: str, event_type: str, payload: dict[str, Any]) -> None:
    """写入审计事件（引用来源 / HITL 事件等）。失败仅告警，不影响主流程。"""
    try:
        pool = await get_pool()
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO audit (session_id, event_type, payload) VALUES (%s, %s, %s)",
                (session_id, event_type, json.dumps(payload, ensure_ascii=False, default=str)),
            )
            await conn.commit()
    except Exception as e:  # pragma: no cover — 审计失败不阻塞主流程
        logger.warning("audit 写入失败: %s", e)


async def upsert_session(session_id: str, user_id: Optional[str] = None,
                         meta: Optional[dict] = None) -> None:
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
                (session_id, user_id, json.dumps(meta or {}, ensure_ascii=False),
                 datetime.now(timezone.utc)),
            )
            await conn.commit()
    except Exception as e:  # pragma: no cover
        logger.warning("session 写入失败: %s", e)


async def record_feedback(session_id: str, rating: int,
                          comment: str = "", answer_snapshot: str = "") -> None:
    """记录用户对回答的反馈。"""
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO feedback (session_id, rating, comment, answer_snapshot) VALUES (%s, %s, %s, %s)",
            (session_id, rating, comment, answer_snapshot[:4000]),
        )
        await conn.commit()


#  P1 观测层落库助手(trace_runs / trace_spans, 规格 2026-09-20-eval-monitoring-spec.md)


def _trace_json(value: Any) -> "Json":
    """JSONB 包装: 非 JSON 原生类型(state 含 Pydantic 模型等)用 default=str 兜住, 全文不截断。"""
    from psycopg.types.json import Json

    return Json(value, dumps=lambda o: json.dumps(o, default=str, ensure_ascii=False))


async def insert_trace_run(run_id: str, session_id: str, run_type: str, mode: str,
                           status: str, query: str, final_answer: str,
                           metrics: dict, started_at: float, ended_at: float) -> None:
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
            (run_id, session_id, run_type, mode, status, query, final_answer,
             _trace_json(metrics), datetime.fromtimestamp(started_at),
             datetime.fromtimestamp(ended_at)),
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
                [(r["run_id"], r["span_type"], r["name"], r["status"],
                  _trace_json(r["input"]), _trace_json(r["output"]),
                  _trace_json(r["state"]), r["latency_ms"],
                  _trace_json(r["token_usage"]), datetime.fromtimestamp(r["started_at"]))
                 for r in rows],
            )
        await conn.commit()
