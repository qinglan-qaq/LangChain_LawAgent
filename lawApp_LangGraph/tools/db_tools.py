"""
长期记忆工具集 — 基于 LangGraph BaseStore (PostgresStore / InMemoryStore)

    search_memory  — 语义搜索过往记忆,召回相关上下文
    save_to_memory — 保存事实/偏好/对话摘要到长期记忆
    fetch_laws     — 法律条文语义检索 (PostgreSQL + pgvector, law_vector 表)

v2 变更 (upgrade-v1):
- 记忆存储从自管 agent_memory 表迁移到 LangGraph BaseStore
  (PostgresStore 持久化 + 1024 维 BGE 向量索引;跨 thread 共享)
- 工具内经 langgraph.config.get_store() 取当前 store,
  仅在图运行上下文中可用;离线调用时优雅降级
- fetch_laws 改为 async psycopg3 连接池
"""

import json
import time
from typing import Any, Optional

from langchain_core.tools import tool

from lawApp_LangGraph.FastAPI.logging import tool as tool_log, system as sys_log
from lawApp_LangGraph.state import LawsResult

#  Store 访问 — 仅在图执行上下文中可用


def _get_store():
    """获取当前 LangGraph store;不在图上下文中时返回 None。"""
    try:
        from langgraph.config import get_store

        return get_store()
    except Exception:
        return None


MEM_NAMESPACE = ("law_agent", "memories")  # store 命名空间
MAX_EMBED_LEN = 512  # 嵌入文本上限, 超出自动截断


# Tool A: 搜索记忆


@tool
async def search_memory(query: str, top_k: int = 3) -> dict:
    """搜索长期记忆库,召回与当前问题相关的历史信息。

    适用场景:
    - 用户提及之前讨论过的话题
    - 需要参考过往的法律偏好或决策
    - 跨会话的上下文补充

    参数:
    query: 搜索查询,描述需要回忆的内容
    top_k: 返回条数,默认 3

    返回:
    dict, 含 memories 列表,每项为 {memory_type, content, created_at, similarity}
    """
    t0 = time.time()
    tool_log.info(
        "→ 调用工具: search_memory",
        detail=f"query={query[:60]} | top_k={top_k}",
    )

    store = _get_store()
    if store is None:
        tool_log.warning(
            "← 工具降级: search_memory", detail="无 store 上下文(离线调用),返回空"
        )
        return {"memory_results": [], "status": "unavailable", "count": 0}

    try:
        items = await store.asearch(
            MEM_NAMESPACE,
            query=query,
            limit=top_k,
        )
    except (TypeError, ValueError):
        # 无向量索引的 store 不支持语义检索 → 退化为取最近条目
        items = await store.asearch(MEM_NAMESPACE, limit=top_k)
    except Exception as e:
        tool_log.error("← 工具异常: search_memory", detail=str(e)[:120])
        return {"memory_results": [], "status": "error", "count": 0}

    memories = [
        {
            "memory_type": (it.item or {}).get("memory_type", "general"),
            "content": it.item.get("content", "") if isinstance(it.item, dict) else str(it.item),
            "created_at": str(it.created_at) if getattr(it, "created_at", None) else "",
            "similarity": round(float(getattr(it, "score", 0) or 0), 4),
        }
        for it in items
    ]

    top_sim = memories[0]["similarity"] if memories else 0
    tool_log.info(
        "← 工具返回: search_memory",
        detail=f"命中{len(memories)}条记忆",
        result=f"top_similarity={top_sim:.3f} | elapsed={time.time() - t0:.2f}s",
    )
    return {
        "memory_results": memories,
        "status": "success" if memories else "empty",
        "count": len(memories),
    }


# Tool B: 保存记忆


@tool
async def save_to_memory(
    content: str,
    memory_type: str = "general",
    summary: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> dict:
    """将重要信息保存到长期记忆库,供未来会话使用。

    关键: content 是完整原文(存入 item), summary 是简短摘要(用于向量检索)。
    请自行提供一句话 summary,避免长文本被嵌入后拉高成本。
    如果不传 summary 且 content 较短(<512字),则直接用 content 做嵌入。

    适用场景:
    - 用户明确表达了偏好或需求
    - 对话中得到了重要的结论或建议
    - 用户告知了个人情况(职业、所在地等)
    - 法律咨询的关键结论

    参数:
    content: 要保存的完整记忆文本
    memory_type: 记忆类型,如 'user_fact' / 'legal_preference' / 'conclusion' / 'general'
    summary: 简短摘要(1-2句),用于语义搜索匹配。不传则用 content 截断
    metadata: 附加元数据,如 {'law_title': '民法典', 'article': '第一千零四十二条'}

    返回:
    dict, 含 status / key / memory_type / is_truncated
    """
    t0 = time.time()
    tool_log.info(
        "→ 调用工具: save_to_memory",
        detail=f"type={memory_type} | content_len={len(content)}",
    )

    store = _get_store()
    if store is None:
        tool_log.warning(
            "← 工具降级: save_to_memory", detail="无 store 上下文(离线调用),跳过保存"
        )
        return {"memory_update": None, "status": "unavailable", "message": "记忆存储不可用"}

    embed_text = (summary or content).strip()
    is_truncated = False
    if len(embed_text) > MAX_EMBED_LEN:
        embed_text = embed_text[:MAX_EMBED_LEN]
        is_truncated = True

    import uuid

    key = f"{memory_type}_{uuid.uuid4().hex[:12]}"

    value = {
        "content": content,
        "summary": embed_text,
        "memory_type": memory_type,
        "full_content": content if summary else None,
        **(metadata or {}),
    }

    try:
        await store.aput(
            MEM_NAMESPACE,
            key,
            value,
        )
    except Exception as e:
        tool_log.error("← 工具异常: save_to_memory", detail=str(e)[:120])
        return {"memory_update": None, "status": "error", "message": str(e)[:200]}

    msg = f"记忆已保存 (key={key}, type={memory_type}"
    if is_truncated:
        msg += f", 嵌入文本已截断至 {MAX_EMBED_LEN} 字"
    msg += ")"

    tool_log.info(
        "← 工具返回: save_to_memory",
        detail=f"key={key} | type={memory_type}" + (" | truncated" if is_truncated else ""),
        result=f"elapsed={time.time() - t0:.2f}s",
    )
    return {
        "memory_update": {
            "key": key,
            "memory_type": memory_type,
            "is_truncated": is_truncated,
            "summary": embed_text,
        },
        "status": "success",
        "message": msg,
    }


# Tool C: 法律条文检索


@tool
async def fetch_laws(query: str, top_k: int = 5) -> dict:
    """从法律条文数据库中语义检索相关法条.使用 PGVector 向量相似度搜索,
    召回与查询问题最相关的法律法规条文,为法律分析提供权威依据.

    适用场景:
    - 需要引用具体法律条文支撑法律意见
    - 查找特定领域的法律法规,不仅限于某部法律
    - 确认某法律问题的适用法条
    - Planner 判断回答需要法律条文依据时优先调用

    参数:
    query: 法律问题或关键词,用于语义匹配相关法条,中文
    top_k: 返回条数,默认 5

    返回:
    dict, 含 law_results 列表,每项为 LawsResult 实例
    """
    t0 = time.time()
    tool_log.info(
        "→ 调用工具: fetch_laws",
        detail=f"query={query[:80]} | top_k={top_k}",
    )

    from lawApp_LangGraph.RAG_service.embedder import embed_query

    try:
        query_vec = await embed_query(query)
        from lawApp_LangGraph.db import get_pool

        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT law_title, chapter, article_number, content,
                1 - (embedding <=> %s::vector) AS similarity
                FROM law_vector
                WHERE embedding IS NOT NULL
                AND status = '现行有效'
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (query_vec, query_vec, top_k),
            )
            rows = await cur.fetchall()
    except Exception as e:
        tool_log.error(
            "← 工具异常: fetch_laws", detail=f"法条库不可用: {str(e)[:120]}"
        )
        return {"law_results": [], "status": "error", "count": 0}

    laws = [
        LawsResult(
            law_title=r[0],
            chapter=r[1] or "",
            article_number=r[2],
            content=(r[3] or "")[:600],
        )
        for r in rows
    ]

    top_sim = float(rows[0][4]) if rows else 0
    tool_log.info(
        "← 工具返回: fetch_laws",
        detail=f"命中{len(laws)}条法条",
        result=f"top_similarity={top_sim:.3f} | elapsed={time.time() - t0:.2f}s",
    )
    return {
        "law_results": laws,
        "status": "success" if laws else "empty",
        "count": len(laws),
    }
