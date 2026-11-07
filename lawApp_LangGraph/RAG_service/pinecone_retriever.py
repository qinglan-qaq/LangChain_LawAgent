"""
Pinecone 检索后端 — 稠密单路 / RRF 混合检索(可切换)

dense 模式(当前默认): BGE 稠密召回 → CrossEncoder 重排 → sigmoid 出分。
hybrid 模式: dense_recall/sparse_recall 双路并发独立召回 → fusion.rrf_fuse
倒数排名融合 → CrossEncoder 重排。混合模式使用
settings.pinecone_hybrid_namespace(未配置时回退 pinecone_namespace),
为后续新建 Pinecone 混合索引 namespace 预留切换位。

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


async def warmup_rag() -> None:
    """后台预热 BGE / CrossEncoder / Pinecone 连接, 不阻塞 API 启动。

    模型加载与首次推理放在线程池; Pinecone 只做 describe_index 连通性检查。
    失败仅记日志, 首个检索请求仍会按懒加载路径重试。
    """
    if not settings.rag_warmup_enabled:
        return

    import asyncio
    import time

    from lawApp_LangGraph.FastAPI.logging import system as system_log

    t0 = time.time()
    system_log.info(
        "RAG 预热开始",
        detail=f"embed={settings.memory_embed_model} | rerank={settings.rerank_model} | backend={settings.retriever_backend}",
    )

    def _warm_models() -> None:
        from lawApp_LangGraph.RAG_service.embedder import get_embedder, get_reranker

        embedder = get_embedder()
        embedder.encode("婚姻家庭法律咨询预热", normalize_embeddings=True)
        reranker = get_reranker()
        if reranker is not None:
            reranker.predict([["婚姻家庭法律咨询预热", "夫妻共同财产分割"]])
        system_log.info("RAG 预热: BGE/CrossEncoder 就绪", detail=f"elapsed={time.time()-t0:.1f}s")

    def _warm_pinecone() -> None:
        if settings.retriever_backend.lower().strip() != "pinecone":
            return
        svc = _get_service()
        svc.pc.describe_index(svc.index_name)
        system_log.info("RAG 预热: Pinecone 就绪", detail=f"index={svc.index_name}")

    results = await asyncio.gather(
        asyncio.to_thread(_warm_models),
        asyncio.to_thread(_warm_pinecone),
        return_exceptions=True,
    )
    for label, result in zip(("models", "pinecone"), results):
        if isinstance(result, Exception):
            logger.warning("RAG 预热 %s 失败(不影响服务): %s", label, result)
            system_log.warning("RAG 预热失败", detail=f"{label}: {str(result)[:160]}")
    system_log.info("RAG 后台预热结束", detail=f"elapsed={time.time()-t0:.1f}s")


class PineconeRetriever(BaseRetriever):
    async def search(
        self,
        query: str,
        top_k: int = 20,
        rerank_top_n: int = 5,
        namespace: Optional[str] = None,
    ) -> list[dict]:
        """按 pinecone_query_mode 执行稠密单路或 RRF 混合检索.

        Args:
            query: 查询文本, 中文。
            top_k: 每路召回条数(钳制 1..50)。
            rerank_top_n: 重排后返回条数(钳制 1..10)。
            namespace: 检索命名空间; None 时 dense 用 pinecone_namespace,
                hybrid 用 pinecone_hybrid_namespace(未配置回退前者)。

        Returns:
            RetrievedDocument 对齐的 dict 列表。rerank 可用时每项含
            rrf_score / relevance_score(sigmoid 重排分) / hybrid_score
            (= relevance_score 兼容别名) / rank(重排名次); rerank 不可
            用时缺 relevance_score 与 hybrid_score 两键, rank 为召回名次
            — 缺键即 evaluate_case_relevance 全 ambiguous 门控的标记
            (spec §5⑤), 排序不骗人。

        Raises:
            RuntimeError: 召回失败(调用方 rag_tools 兜 status=error)。
        """
        import asyncio

        from lawApp_LangGraph.RAG_service.embedder import rerank
        from lawApp_LangGraph.RAG_service.fusion import (
            clamp_search_params,
            rrf_fuse,
            sigmoid,
        )

        hybrid = settings.pinecone_query_mode.lower().strip() == "hybrid"
        default_ns = (
            settings.pinecone_hybrid_namespace
            if hybrid and settings.pinecone_hybrid_namespace
            else settings.pinecone_namespace
        )
        ns = namespace or default_ns
        top_k, rerank_top_n = clamp_search_params(top_k, rerank_top_n)
        svc = _get_service()

        dense_m: list = []
        sparse_m: list = []
        if hybrid:
            # 双路并发召回(SDK 同步, 各自让出事件循环); 单路失败降级单路
            results = await asyncio.gather(
                asyncio.to_thread(svc.dense_recall, query, ns, top_k),
                asyncio.to_thread(svc.sparse_recall, query, ns, top_k),
                return_exceptions=True,
            )
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
        else:
            # 当前 Pinecone 索引为纯稠密索引, 纯稀疏查询会被 400 拒绝
            dense_m = await asyncio.to_thread(svc.dense_recall, query, ns, top_k)
            if not dense_m:
                raise RuntimeError("稠密召回失败或为空")

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
