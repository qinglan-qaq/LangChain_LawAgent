"""
pgvector 检索后端 — 本地 PostgreSQL 案例检索

流程: BGE 密集向量 → cosine 召回 top_k → (可选) CrossEncoder 重排序取 top_n。
注意: 纯密集检索，alpha 参数不生效（仅为接口兼容保留）；
数据需先经 scripts/ingest_cases_pgvector.py 入库 law_cases 表。
"""

from __future__ import annotations

import logging

from lawApp_LangGraph.RAG_service.base import BaseRetriever
from lawApp_LangGraph.RAG_service.embedder import embed_query, rerank

logger = logging.getLogger("lawApp.rag")


class PgvectorRetriever(BaseRetriever):
    async def search(
        self,
        query: str,
        top_k: int = 20,
        rerank_top_n: int = 5,
        alpha: float = 0.4,  # noqa: ARG002 — 接口兼容，pgvector 为纯密集检索
        namespace: Optional[str] = None,  # noqa: ARG002 — pgvector 以表为单位，无 namespace
    ) -> list[dict]:
        from lawApp_LangGraph.db import get_pool

        qvec = await embed_query(query)
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT id, year, case_number, case_cause, chunk_text,
                       1 - (embedding <=> %s::vector) AS hybrid_score
                FROM law_cases
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (qvec, qvec, int(top_k)),
            )
            rows = await cur.fetchall()

        results = [
            {
                "id": r[0],
                "year": r[1] or "",
                "case_number": r[2] or "",
                "case_cause": r[3] or "",
                "chunk_text": (r[4] or "")[:500],
                "hybrid_score": round(float(r[5]), 4),
            }
            for r in rows
        ]

        if not results:
            return []

        # 重排序（RERANK_ENABLED=0 或失败时保留召回排序）
        scores = await rerank(query, results)
        if scores is not None and len(scores) == len(results):
            order = sorted(range(len(results)), key=lambda i: scores[i], reverse=True)
            results = [results[i] for i in order[:rerank_top_n]]
            for rank, doc in enumerate(results, start=1):
                doc["rank"] = rank
        else:
            results = sorted(results, key=lambda d: d["hybrid_score"], reverse=True)[
                :rerank_top_n
            ]
            for rank, doc in enumerate(results, start=1):
                doc["rank"] = rank

        logger.info(
            "pgvector 检索完成 | 召回→重排→返回 %d 条 | top_score=%.3f",
            len(results),
            results[0]["hybrid_score"] if results else 0,
        )
        return results
