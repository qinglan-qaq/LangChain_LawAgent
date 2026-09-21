"""
MCP Server — law-search (自建法律检索服务, A2)

对外暴露 3 个 MCP 工具(复用 Agent 本地工具的底层实现):
    search_laws    — 法律条文语义检索 (law_vector 表, pgvector)
    search_cases   — 案例混合检索 (Retriever 抽象层)
    recall_memory  — 长期记忆召回 (LangGraph Store)

传输: streamable-http, 默认 http://127.0.0.1:9381/mcp
      (Cursor / 任意 MCP client 均可直接挂载)

实现说明:
- 服务端工具不依赖图执行上下文(不使用 get_store());记忆召回
  直连独立 InMemoryStore/PostgresStore,与图内 store 同一持久化后端
- 工具参数收敛为 query/top_k 简单标量,MCP client 无需了解内部 schema

运行:
    python -m lawApp_LangGraph.mcp.mcp_server
    # 或指定端口: MCP_PORT=9382 python -m lawApp_LangGraph.mcp.mcp_server

Cursor 挂载见 .cursor/mcp.json
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from mcp.server.fastmcp import FastMCP

from lawApp_LangGraph.config import settings

# 工具底层走 psycopg 异步连接池, 必须跑在 SelectorEventLoop 上;
# MCP 子进程/服务的循环由 mcp.run() 在导入之后才创建, db.py 的
# 模块级 policy 切换来得太晚(Proactor 循环下 psycopg 池会死等) → 此处提前切
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# MCP stdio 子进程按 SDK 白名单继承 env, 可能缺 USERNAME;
# torch dynamo 初始化走 getpass.getuser(), 缺失时在 Windows 上
# import pwd(Unix-only) → ModuleNotFoundError → transformers 全线不可用
os.environ.setdefault("USERNAME", "mcp-child")


def _preload_native_deps() -> None:
    """在 mcp.run() 启动 stdin 读取线程之前, 导入并预热全部重量级依赖。

    Windows 实测死锁: stdio 传输下 MCP SDK 有工作线程阻塞在 stdin 的
    同步读(os.read / BufferedReader.read)上, 此时任何线程再 LoadLibrary
    新的 C 扩展(numpy/torch/psycopg 的 .pyd)会与该阻塞读互锁 → 子进程
    在首次工具调用时挂死(见 P0-P1 执行记录)。工具内部均为函数级懒导入,
    因此必须在模块导入期、即 stdin 读取线程尚不存在时一次性导入完:
        - db / db_tools / rag_tools / tools: psycopg 等全部工具链依赖
        - get_embedder(): numpy/torch + BGE 模型常驻, 调用期不再加载 DLL
    """
    from lawApp_LangGraph import db  # noqa: F401  psycopg C 扩展
    from lawApp_LangGraph.RAG_service.embedder import get_embedder
    from lawApp_LangGraph.tools import db_tools, rag_tools, tools  # noqa: F401

    get_embedder()


_preload_native_deps()

logger = logging.getLogger("lawApp.mcp")

mcp = FastMCP("law-search")


#  服务器侧 Store — 独立实例(不在图上下文,不能 get_store())

_store = None
# M7: 双建防护 —— 并发首调时防两个协程各建一个 store
_store_lock = asyncio.Lock()


class MemoryStoreUnavailable(RuntimeError):
    """MCP server 侧记忆库不可用(M7)。

    PG store setup 失败时不再静默换 InMemoryStore —— 图写 PG store、
    server 读 InMemory, 记忆互不可见且零信号。显式失败让记忆工具返回
    「记忆库暂不可用」, 调用方可感知。
    """


async def _close_store_pool(store) -> None:
    """尽力关闭 store 半初始化持有的连接池(防泄漏);池属性随版本浮动。"""
    for attr in ("conn", "pool", "_pool"):
        pool = getattr(store, attr, None)
        if pool is not None and not isinstance(pool, str) and hasattr(pool, "close"):
            try:
                await pool.close()
            except Exception:  # pragma: no cover — 观测旁路尽力而为
                pass
            return


async def _get_server_store():
    """MCP server 自用的 store(Postgres;M7: 失败显式报错, 不降级 InMemory)。
    与图的 store 分属两个进程,同一 Postgres 时记忆互通。"""
    global _store
    if _store is not None:
        return _store
    async with _store_lock:
        if _store is not None:
            return _store
        pg_store = None
        try:
            from langgraph.store.postgres.aio import AsyncPostgresStore

            from lawApp_LangGraph.RAG_service.embedder import embed_fn_for_store
            from lawApp_LangGraph.db import build_dsn

            pg_store = AsyncPostgresStore(
                conn=None,  # AutoPoolConn: 传 None 时内部自动建池
                index={"dims": 1024, "embed": embed_fn_for_store,
                       "fields": ["summary", "content"]},
            )
            await pg_store.setup()
            _store = pg_store
            logger.info("MCP store: Postgres")
        except Exception as e:
            # M7: PG store 不可用 → 显式失败。静默换 InMemory 会让图写 PG、
            # server 读内存, 记忆互不可见零信号; 半初始化的连接池关掉防泄漏
            logger.warning("MCP Postgres store 不可用, 记忆工具将返回错误: %s",
                           str(e)[:120])
            if pg_store is not None:
                await _close_store_pool(pg_store)
            raise MemoryStoreUnavailable(str(e)[:200])
    return _store


MEM_NAMESPACE = ("law_agent", "memories")  # 与图内记忆工具同命名空间


#  MCP 工具定义 — 参数刻意保持简单(MCP client 零知识即可调用)


@mcp.tool()
async def search_laws(query: str, top_k: int = 5) -> str:
    """语义检索中国法律条文.返回最相关的现行有效法条(法规名/条款号/原文).

    Args:
        query: 法律问题或关键词,中文
        top_k: 返回条数,默认 5
    """
    from lawApp_LangGraph.tools.db_tools import fetch_laws

    result = await fetch_laws.ainvoke({"query": query, "top_k": top_k})
    laws = result.get("law_results", []) if isinstance(result, dict) else []
    status = result.get("status", "error") if isinstance(result, dict) else "error"

    if status == "error":
        return "法条库暂不可用,请稍后再试。"
    if not laws:
        return "未检索到相关法条。"

    lines = [f"共检索到 {len(laws)} 条相关法条:", ""]
    for law in laws:
        if isinstance(law, dict):
            lines.append(
                f"《{law.get('law_title', '')}》{law.get('chapter', '')} "
                f"第{law.get('article_number', '')}条\n{law.get('content', '')}"
            )
        else:
            lines.append(
                f"《{law.law_title}》{law.chapter} 第{law.article_number}条\n{law.content}"
            )
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
async def search_cases(query: str, top_k: int = 5) -> str:
    """检索历史判例案例库.返回最相关案例的案号/年份/案情要点.

    Args:
        query: 法律问题,中文
        top_k: 返回案例条数,默认 5
    """
    from lawApp_LangGraph.tools.rag_tools import retrieve_legal_knowledge

    result = await retrieve_legal_knowledge.ainvoke({"query": query, "top_k": 20})
    docs = result.get("rag_documents", []) if isinstance(result, dict) else []
    status = result.get("status", "error") if isinstance(result, dict) else "error"

    if status == "error":
        return "案例库暂不可用,请稍后再试。"
    if not docs:
        return "未检索到相关案例。"

    lines = [f"共检索到 {len(docs)} 个相关案例:", ""]
    for doc in docs[:top_k]:
        if isinstance(doc, dict):
            lines.append(
                f"[{doc.get('year', '')}年 | 案号:{doc.get('case_number', '未知')}] "
                f"混合评分 {doc.get('hybrid_score', 0):.2f}\n{doc.get('chunk_text', '')}"
            )
        else:
            lines.append(
                f"[{doc.year}年 | 案号:{doc.case_number}] "
                f"混合评分 {doc.hybrid_score:.2f}\n{doc.chunk_text}"
            )
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
async def recall_memory(query: str, top_k: int = 3) -> str:
    """召回历史咨询的长期记忆(用户偏好/既往结论/个人情况).

    Args:
        query: 要回忆的内容描述
        top_k: 返回条数,默认 3
    """
    # M7: PG store 不可用 → 显式错误文案, 不静默回 InMemory 空结果
    try:
        store = await _get_server_store()
    except MemoryStoreUnavailable:
        return "记忆库暂不可用,请稍后再试。"
    try:
        items = await store.asearch(MEM_NAMESPACE, query=query, limit=top_k)
    except (TypeError, ValueError):
        items = await store.asearch(MEM_NAMESPACE, limit=top_k)
    except Exception:
        return "记忆库暂不可用。"

    if not items:
        return "未找到相关记忆。"

    lines = [f"召回 {len(items)} 条相关记忆:", ""]
    for it in items:
        item = it.item if isinstance(it.item, dict) else {}
        content = item.get("content", "") or item.get("summary", "")
        if not content and not isinstance(it.item, dict):
            content = str(it.item)
        lines.append(f"[{item.get('memory_type', 'general')}] {content}")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    """启动 law-search MCP server (streamable-http)."""
    logging.basicConfig(level=logging.INFO)
    port = settings.mcp_port
    host = settings.mcp_host
    # streamable-http 端点为 {settings.streamable_http_path} = /mcp
    logger.info("law-search MCP server 启动: http://%s:%s/mcp", host, port)
    mcp.settings.host = host
    mcp.settings.port = port
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
