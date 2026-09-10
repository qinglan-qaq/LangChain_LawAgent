"""
本地模型懒加载单例 — BGE 嵌入 + BGE 重排序

供 pgvector 检索、法律条文检索、PostgresStore 记忆索引共用，
避免各处重复加载 ~1GB 级别的模型。
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from lawApp_LangGraph.config import settings

logger = logging.getLogger("lawApp.rag")

_embedder = None
_embedder_lock = threading.Lock()
_reranker = None
_reranker_lock = threading.Lock()


def get_embedder():
    """SentenceTransformer BGE 嵌入模型（懒加载单例）。"""
    global _embedder
    if _embedder is None:
        with _embedder_lock:
            if _embedder is None:
                from sentence_transformers import SentenceTransformer

                logger.info(
                    "初始化 Embedder (冷启动) | model=%s", settings.memory_embed_model
                )
                _embedder = SentenceTransformer(settings.memory_embed_model)
    return _embedder


def get_reranker():
    """CrossEncoder 重排序模型（懒加载单例，RERANK_ENABLED=0 可禁用）。"""
    global _reranker
    if _reranker is None:
        with _reranker_lock:
            # 禁用分支不缓存单例, 后续调用仍可重新判定
            if _reranker is None:
                if settings.rerank_enabled == "0":
                    return None
                from sentence_transformers import CrossEncoder

                logger.info(
                    "初始化 CrossEncoder (冷启动) | model=%s", settings.rerank_model
                )
                _reranker = CrossEncoder(settings.rerank_model, max_length=512)
    return _reranker


def embed_query_sync(text: str) -> list[float]:
    """同步嵌入单条文本（归一化）。"""
    return get_embedder().encode(text, normalize_embeddings=True).tolist()


async def embed_query(text: str) -> list[float]:
    """嵌入单条文本（同步模型调用，放到线程池避免阻塞事件循环）。"""
    import asyncio

    return await asyncio.to_thread(embed_query_sync, text)


async def rerank(query: str, docs: list[dict], text_key: str = "chunk_text") -> Optional[list[float]]:
    """对 docs 打重排序分；禁用或失败时返回 None（调用方退回原排序）。"""
    reranker = get_reranker()
    if reranker is None or not docs:
        return None
    import asyncio

    pairs = [[query, d.get(text_key, "")] for d in docs]
    try:
        scores = await asyncio.to_thread(reranker.predict, pairs)
        return [float(s) for s in scores]
    except Exception as e:  # pragma: no cover
        logger.warning("重排序失败，退回原始排序: %s", e)
        return None


def embed_fn_for_store(text: str) -> list[float]:
    """PostgresStore index 用嵌入函数（store 同步调用）。"""
    return embed_query_sync(text)
