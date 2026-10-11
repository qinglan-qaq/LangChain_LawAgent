"""RRF(Reciprocal Rank Fusion)融合层 — 纯函数, 无 IO.

双路召回的客户端融合: rrf(d) = Σ w_route / (k + rank_route(d)),
只用排名不用原始分, 天然免疫稠密(余弦)与稀疏(BM25)的量纲差异。
同时承载 sigmoid 归一化与检索参数钳制, 供两个检索后端共用。
设计: docs/superpowers/specs/2026-10-11-rrf-hybrid-retrieval-design.md §5②
"""

from __future__ import annotations

import math
from typing import Any, Optional


def sigmoid(x: float) -> float:
    """CrossEncoder logits → [0,1] 相关性分.

    Args:
        x: CrossEncoder 原始输出(logits, 无界)。

    Returns:
        sigmoid 映射后的 [0,1] 分数, 保留 4 位小数。
        模型绝对输出映射, 跨查询可比(否决批内 min-max: 查询相对分漂移)。
    """
    return round(1.0 / (1.0 + math.exp(-x)), 4)


def clamp_search_params(top_k: int, rerank_top_n: int) -> tuple[int, int]:
    """检索参数钳制(M1): top_k → 1..50, rerank_top_n → 1..10.

    0/负数不再把重排序切片切成空列表(假「未检索到」); 非法类型回落
    默认 20/5。两个检索后端共用。

    Args:
        top_k: 每路召回条数。
        rerank_top_n: 重排后返回条数。

    Returns:
        (钳制后的 top_k, 钳制后的 rerank_top_n)。
    """
    try:
        k = int(top_k)
    except (TypeError, ValueError):
        k = 20
    try:
        n = int(rerank_top_n)
    except (TypeError, ValueError):
        n = 5
    return max(1, min(k, 50)), max(1, min(n, 10))


def _match_id(m: Any) -> Optional[str]:
    """取召回项的向量 id(Pinecone Match 对象或 dict 兼容)."""
    mid = getattr(m, "id", None)
    if mid is None and isinstance(m, dict):
        mid = m.get("id")
    return str(mid) if mid is not None else None


def rrf_fuse(
    dense_matches: list,
    sparse_matches: list,
    k: int = 60,
    w_dense: float = 1.0,
    w_sparse: float = 1.0,
    top_n: int = 20,
) -> list[dict]:
    """双路召回按向量 id 聚合做 RRF 融合.

    Args:
        dense_matches: 稠密路召回列表(Pinecone Match 或 dict), 已按本路得分排序。
        sparse_matches: 稀疏路召回列表, 同上。
        k: RRF 平滑常数, 默认 60(业界惯例)。
        w_dense: 稠密路权重, 默认 1.0。
        w_sparse: 稀疏路权重, 默认 1.0。
        top_n: 融合后保留的候选条数上限。

    Returns:
        按 rrf_score 降序的候选列表, 每项为
        {"match": 原始召回项, "id": 向量id, "dense_rank": 名次或None,
         "sparse_rank": 名次或None, "rrf_score": 融合分, "rrf_rank": 融合名次}。
        rrf_score 值域 (0, (w_dense+w_sparse)/(k+1)]; 仅单路出现的文档
        另一路贡献 0 分且对应 rank 为 None。双空输入返回 []。
    """
    fused: dict[str, dict] = {}

    def _absorb(matches: list, weight: float, rank_key: str) -> None:
        for rank, m in enumerate(matches, start=1):
            mid = _match_id(m)
            if mid is None:
                continue
            slot = fused.setdefault(
                mid,
                {
                    "match": m,
                    "id": mid,
                    "dense_rank": None,
                    "sparse_rank": None,
                    "rrf_score": 0.0,
                },
            )
            slot[rank_key] = rank
            slot["rrf_score"] += weight / (k + rank)

    _absorb(dense_matches, w_dense, "dense_rank")
    _absorb(sparse_matches, w_sparse, "sparse_rank")

    ordered = sorted(fused.values(), key=lambda d: d["rrf_score"], reverse=True)
    kept = ordered[: max(1, int(top_n))]
    for rank, slot in enumerate(kept, start=1):
        slot["rrf_rank"] = rank
        slot["rrf_score"] = round(float(slot["rrf_score"]), 6)
    return kept
