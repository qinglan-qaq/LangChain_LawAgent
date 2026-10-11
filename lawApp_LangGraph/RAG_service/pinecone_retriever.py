"""
Pinecone 检索后端 — RRF 混合检索(双路独立召回 + 客户端融合 + CrossEncoder 出分)

流程: dense_recall/sparse_recall 双路并发独立召回 → fusion.rrf_fuse 倒数排名
融合 → embedder.rerank(CrossEncoder)重排 → sigmoid 出分(relevance_score)。
复用 legacy RAG_service 的嵌入/BM25 管线, 但:
    - 懒加载(首次 search 时才初始化模型与连接)
    - 不在检索路径上自动建索引(ingest 脚本负责)
    - 返回统一 dict 格式
设计: docs/superpowers/specs/2026-10-11-rrf-hybrid-retrieval-design.md §5
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

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
                # H6: 局部变量构造 —— 全局单例仅在构造+index 附着全部成功后才赋值;
                # 半初始化失败会污染 _service, 之后每次 RuntimeError("索引未初始化")
                # 直到重启
                try:
                    svc = RAG_service(
                        index_name=settings.pinecone_index_name,
                        api_key=settings.pinecone_api_key,  # type: ignore[arg-type]
                        cloud=settings.pinecone_cloud,
                        region=settings.pinecone_region,
                    )
                    # 检索路径只附着到已存在的索引，不创建
                    svc.index = svc.pc.Index(svc.index_name)
                except Exception:
                    logger.warning("Pinecone RAG_service 初始化失败", exc_info=True)
                    raise
                _service = svc
    return _service


class PineconeRetriever(BaseRetriever):
    async def search(
        self,
        query: str,
        top_k: int = 20,
        rerank_top_n: int = 5,
        namespace: Optional[str] = None,
    ) -> list[dict]:
        """RRF 混合检索编排: 双路并发召回 → 客户端融合 → CrossEncoder 出分.

        Args:
            query: 查询文本, 中文。
            top_k: 每路召回条数(钳制 1..50)。
            rerank_top_n: 重排后返回条数(钳制 1..10)。
            namespace: 检索命名空间, None 时以 settings.pinecone_namespace 兜底。

        Returns:
            RetrievedDocument 对齐的 dict 列表。rerank 可用时每项含
            rrf_score / relevance_score(sigmoid 重排分) / hybrid_score
            (= relevance_score 兼容别名) / rank(重排名次); rerank 不可
            用时缺 relevance_score 与 hybrid_score 两键, rank 为 RRF 名次
            — 缺键即 evaluate_case_relevance 全 ambiguous 门控的标记
            (spec §5⑤), 排序不骗人。

        Raises:
            RuntimeError: 双路召回全部失败(调用方 rag_tools 兜 status=error)。
        """
        import asyncio

        from lawApp_LangGraph.RAG_service.embedder import rerank
        from lawApp_LangGraph.RAG_service.fusion import (
            clamp_search_params,
            rrf_fuse,
            sigmoid,
        )

        ns = namespace or settings.pinecone_namespace
        top_k, rerank_top_n = clamp_search_params(top_k, rerank_top_n)
        svc = _get_service()

        # 双路并发召回(SDK 同步, 各自让出事件循环); 单路失败降级单路
        results = await asyncio.gather(
            asyncio.to_thread(svc.dense_recall, query, ns, top_k),
            asyncio.to_thread(svc.sparse_recall, query, ns, top_k),
            return_exceptions=True,
        )
        dense_m, sparse_m = [], []
        if isinstance(results[0], list):
            dense_m = results[0]
        else:
            logger.warning("稠密路召回失败, 降级稀疏单路: %s", results[0])
        if isinstance(results[1], list):
            sparse_m = results[1]
        else:
            logger.warning("稀疏路召回失败, 降级稠密单路: %s", results[1])
        if not dense_m and not sparse_m:
            raise RuntimeError(f"双路召回全部失败: {results!r}")

        candidates = rrf_fuse(
            dense_m,
            sparse_m,
            k=settings.rrf_k,
            w_dense=settings.rrf_w_dense,
            w_sparse=settings.rrf_w_sparse,
            top_n=max(2 * rerank_top_n, 20),
        )

        def _meta(m) -> dict:
            return getattr(m, "metadata", None) or {}

        docs = [
            {
                "id": c["id"],
                "year": _meta(c["match"]).get("year", ""),
                "case_number": _meta(c["match"]).get("case_number", ""),
                "case_cause": _meta(c["match"]).get("case_cause", ""),
                "chunk_text": (_meta(c["match"]).get("chunk_text", "") or "")[:500],
                "rrf_score": round(float(c["rrf_score"]), 6),
            }
            for c in candidates
        ]

        scores = await rerank(query, docs)
        if scores is not None and len(scores) == len(docs):
            order = sorted(range(len(docs)), key=lambda i: scores[i], reverse=True)
            kept = order[:rerank_top_n]
            docs = [
                {**docs[i], "relevance_score": sigmoid(scores[i]),
                 "hybrid_score": sigmoid(scores[i])}
                for i in kept
            ]
            logger.info(
                "RRF 检索完成(重排) | 候选%d → 返回%d 条 | top_relevance=%.4f",
                len(candidates), len(docs), docs[0]["relevance_score"] if docs else 0,
            )
        else:
            # rerank 不可用: 不写 relevance_score/hybrid_score — 缺键即
            # evaluate_case_relevance 全 ambiguous + verdict 不足 的降级标记
            docs = docs[:rerank_top_n]
            logger.warning("重排不可用, 排序退 RRF 序 | 返回%d 条", len(docs))

        for rank, d in enumerate(docs, start=1):
            d["rank"] = rank
        return docs
