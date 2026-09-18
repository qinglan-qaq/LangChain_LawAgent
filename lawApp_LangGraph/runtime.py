"""
运行时接线 — checkpointer / store / graph 的装配与降级链

装配优先级:
    CHECKPOINT_BACKEND=postgres → AsyncPostgresSaver + AsyncPostgresStore(连接池)
    (默认 / postgres 不可用时)  → MemorySaver + InMemoryStore(带 BGE 向量索引)

PG 不依赖 docker:使用本机 PostgreSQL 服务(经 DATABASE_URL 或 DB_* 配置),
容器方案见 docker-compose(A4)。

资源持有策略:Postgres 装配后连接池由本模块持有,teardown_runtime() 时关闭;
应用生命周期内单例,重复调用 setup_runtime 幂等。
"""

from __future__ import annotations

import logging

from lawApp_LangGraph.config import settings

logger = logging.getLogger("lawApp.runtime")

graph = None
checkpoint_backend: str = "unknown"

# Postgres 资源持有(模块级,避免被 GC 断连)
_pg_resources: list = []


async def setup_runtime() -> None:
    """装配 graph(checkpointer + store),写回 LangGraph_lawApp 单例。

    FastAPI lifespan 中调用;Postgres 失败自动降级 InMemory,保证服务可启动。
    """
    global graph, checkpoint_backend

    import lawApp_LangGraph.LangGraph_lawApp as app
    from lawApp_LangGraph.mcp.mcp_client import get_mcp_tools
    from lawApp_LangGraph.tools import register_mcp_tools

    if graph is not None:
        return  # 幂等

    # ── MCP 工具挂载(A2): server 不在线时返回空列表, 图照常装配 ──
    mcp_tools = await get_mcp_tools()
    added = register_mcp_tools(mcp_tools)
    if added:
        logger.info("MCP 工具已注入 ALL_TOOLS | %s", [t.name for t in added])

    backend = settings.checkpoint_backend.lower()
    checkpointer = None
    store = None

    if backend in ("postgres", "auto"):
        try:
            checkpointer, store = await _setup_postgres()
            checkpoint_backend = "postgres"
        except Exception as e:
            level = logger.warning if backend == "postgres" else logger.info
            level("Postgres 装配失败,降级 InMemory: %s", str(e)[:150])

    if checkpointer is None:
        from langgraph.checkpoint.memory import MemorySaver
        from langgraph.store.memory import InMemoryStore

        from lawApp_LangGraph.RAG_service.embedder import embed_fn_for_store

        checkpointer = MemorySaver()
        store = InMemoryStore(
            index={
                "dims": 1024,
                "embed": embed_fn_for_store,
                "fields": ["summary", "content"],
            }
        )
        checkpoint_backend = "inmemory"
        logger.info("使用 InMemory checkpointer + InMemoryStore(BGE 1024 维索引)")

    graph = app.build_graph(checkpointer=checkpointer, store=store)
    app.set_graph(graph)  # utils / api 经 app.get_graph() 取同一实例
    logger.info("LangGraph 装配完成 | backend=%s", checkpoint_backend)


async def _setup_postgres():
    """构建 AsyncPostgresSaver + AsyncPostgresStore(各用独立连接池);失败抛异常。"""
    from psycopg import AsyncConnection
    from psycopg_pool import AsyncConnectionPool

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.store.postgres.aio import AsyncPostgresStore

    from lawApp_LangGraph.RAG_service.embedder import embed_fn_for_store
    from lawApp_LangGraph.db import build_dsn

    dsn = build_dsn()

    # ── checkpointer:AsyncPostgresSaver 直接受 AsyncConnectionPool ──
    saver_pool = AsyncConnectionPool(
        conninfo=dsn,
        min_size=1,
        max_size=5,
        open=False,
        timeout=5,  # PG 不可用时快速失败 → 降级 InMemory,而非长时间阻塞启动
    )
    await saver_pool.open()
    checkpointer = AsyncPostgresSaver(conn=saver_pool)
    await checkpointer.setup()
    _pg_resources.append(saver_pool)

    # ── store:接受 conn(单连接或池) + index 配置 ──
    store_pool = AsyncConnectionPool(
        conninfo=dsn,
        min_size=1,
        max_size=5,
        open=False,
        timeout=5,
    )
    await store_pool.open()
    store = AsyncPostgresStore(
        conn=store_pool,
        index={
            "dims": 1024,
            "embed": embed_fn_for_store,
            "fields": ["summary", "content"],
        },
    )
    await store.setup()
    _pg_resources.append(store_pool)

    return checkpointer, store


async def teardown_runtime() -> None:
    """关闭 Postgres 连接池与 MCP 会话(InMemory 后端无需清理)。"""
    global _pg_resources, graph
    for pool in _pg_resources:
        try:
            await pool.close()
        except Exception as e:  # pragma: no cover
            logger.warning("连接池关闭异常: %s", e)
    _pg_resources = []

    from lawApp_LangGraph.mcp.mcp_client import close_mcp

    await close_mcp()
    logger.info("运行时已清理 | backend=%s", checkpoint_backend)
