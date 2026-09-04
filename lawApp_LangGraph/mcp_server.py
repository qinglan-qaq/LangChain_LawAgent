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
    python -m lawApp_LangGraph.mcp_server
    # 或指定端口: MCP_PORT=9382 python -m lawApp_LangGraph.mcp_server

Cursor 挂载见 .cursor/mcp.json
"""

from __future__ import annotations

import logging
import os

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("lawApp.mcp")

mcp = FastMCP("law-search")


#  服务器侧 Store — 独立实例(不在图上下文,不能 get_store())

_store = None


async def _get_server_store():
    """MCP server 自用的 store(优先 Postgres,降级 InMemory)。
    与图的 store 分属两个进程,同一 Postgres 时记忆互通。"""
    global _store
    if _store is not None:
        return _store
    try:
        from langgraph.store.postgres.aio import AsyncPostgresStore

        from lawApp_LangGraph.RAG_service.embedder import embed_fn_for_store
        from lawApp_LangGraph.db import build_dsn

        _store = AsyncPostgresStore(
            conn=None,  # AutoPoolConn: 传 None 时内部自动建池
            index={"dims": 1024, "embed": embed_fn_for_store,
                   "fields": ["summary", "content"]},
        )
        await _store.setup()
        logger.info("MCP store: Postgres")
    except Exception as e:
        logger.warning("MCP store 降级 InMemory: %s", str(e)[:120])
        from langgraph.store.memory import InMemoryStore

        from lawApp_LangGraph.RAG_service.embedder import embed_fn_for_store

        _store = InMemoryStore(
            index={"dims": 1024, "embed": embed_fn_for_store,
                   "fields": ["summary", "content"]}
        )
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
    store = await _get_server_store()
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
    port = int(os.getenv("MCP_PORT", "9381"))
    host = os.getenv("MCP_HOST", "127.0.0.1")
    # streamable-http 端点为 {settings.streamable_http_path} = /mcp
    logger.info("law-search MCP server 启动: http://%s:%s/mcp", host, port)
    mcp.settings.host = host
    mcp.settings.port = port
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
