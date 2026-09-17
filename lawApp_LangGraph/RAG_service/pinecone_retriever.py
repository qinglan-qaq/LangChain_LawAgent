"""
Pinecone 检索后端 — 混合检索（密集 + BM25 稀疏 + CrossEncoder 重排序）

复用 legacy RAG_service 的检索管线，但:
    - 懒加载（首次 search 时才初始化模型与连接）
    - 不在检索路径上自动建索引（ingest 脚本负责）
    - 返回统一 dict 格式
"""

from __future__ import annotations

import logging
import threading

from lawApp_LangGraph.RAG_service.base import BaseRetriever
from lawApp_LangGraph.config import settings

logger = logging.getLogger("lawApp.rag")

_service = None
_service_lock = threading.Lock()


def _get_service():
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                from lawApp_LangGraph.RAG_service.RAG_program import RAG_service

                logger.info("初始化 Pinecone RAG_service (冷启动)")
                _service = RAG_service(
                    index_name=settings.pinecone_index_name,
                    api_key=settings.pinecone_api_key,  # type: ignore[arg-type]
                    cloud=settings.pinecone_cloud,
                    region=settings.pinecone_region,
                )
                # 检索路径只附着到已存在的索引，不创建
                _service.index = _service.pc.Index(_service.index_name)
    return _service


class PineconeRetriever(BaseRetriever):
    async def search(
        self,
        query: str,
        top_k: int = 20,
        rerank_top_n: int = 5,
        alpha: float = 0.4,
        namespace: str = "law_cases",
    ) -> list[dict]:
        import asyncio

        service = _get_service()
        # RAG_service.search_withDenseSparse 为同步重计算（嵌入+重排），
        # 放线程池执行避免阻塞事件循环
        matches = await asyncio.to_thread(
            service.search_withDenseSparse,
            query=query,
            namespace=namespace,
            top_k=top_k,
            rerank_top_n=rerank_top_n,
            alpha=alpha,
        )

        results = []
        for m in matches:
            meta = m.metadata or {}
            results.append(
                {
                    "id": m.id,
                    "year": meta.get("year", ""),
                    "case_number": meta.get("case_number", ""),
                    "case_cause": meta.get("case_cause", ""),
                    "chunk_text": (meta.get("chunk_text", "") or "")[:500],
                    "hybrid_score": round(float(getattr(m, "score", 0) or 0), 4),
                }
            )
        results.sort(key=lambda d: d["hybrid_score"], reverse=True)
        for rank, doc in enumerate(results, start=1):
            doc["rank"] = rank
        return results
