"""
检索器抽象层 — 案例检索的统一入口

后端由 RETRIEVER_BACKEND 环境变量切换:
    pgvector (默认) — 本地 PostgreSQL + pgvector（零云资源，数据经
                      scripts/ingest_cases_pgvector.py 入库）
    pinecone        — Pinecone 混合检索（密集 + BM25 稀疏，需云凭据）

两个后端返回统一格式: list[dict]，字段与 state.RetrievedDocument 对齐
(rank / id / hybrid_score / year / case_number / case_cause / chunk_text)。
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from typing import Optional

logger = logging.getLogger("lawApp.rag")

_retriever: Optional["BaseRetriever"] = None


class BaseRetriever(ABC):
    """案例检索后端接口."""

    @abstractmethod
    async def search(
        self,
        query: str,
        top_k: int = 20,
        rerank_top_n: int = 5,
        alpha: float = 0.4,
        namespace: Optional[str] = None,
    ) -> list[dict]:
        """检索与 query 相关的案例块，返回 RetrievedDocument 对齐的 dict 列表.

        namespace 为 None 时由实现方以 settings.pinecone_namespace 兜底
        (env PINECONE_NAMESPACE 参数化, 见 config.py)。
        """
        raise NotImplementedError

    async def health_check(self) -> bool:
        try:
            await self.search("__health_check__", top_k=1, rerank_top_n=1)
            return True
        except Exception:
            return False


def get_retriever() -> BaseRetriever:
    """按环境变量返回检索后端单例（懒加载）。"""
    global _retriever
    if _retriever is None:
        backend = os.getenv("RETRIEVER_BACKEND", "pgvector").lower().strip()
        if backend == "pinecone":
            from lawApp_LangGraph.RAG_service.pinecone_retriever import (
                PineconeRetriever,
            )

            logger.info("检索后端: Pinecone (混合检索)")
            _retriever = PineconeRetriever()
        elif backend == "pgvector":
            from lawApp_LangGraph.RAG_service.pgvector_retriever import (
                PgvectorRetriever,
            )

            logger.info("检索后端: pgvector (本地)")
            _retriever = PgvectorRetriever()
        else:
            raise ValueError(f"未知检索后端: {backend} (可选: pgvector / pinecone)")
    return _retriever
