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

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from lawApp_LangGraph.config import settings

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
