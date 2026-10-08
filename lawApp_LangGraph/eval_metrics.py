"""检索评测指标(P2 落地) — 纯函数, hit_rate/MRR 为主, P/R/F1 一并输出。

已知局限(spec 记录): golden set 单条 ground truth 时 recall@k∈{0,1}、
precision@k 退化近似 hit_rate; 多相关案例标注扩展后自动有意义。
"""

from __future__ import annotations


def _hits(case: dict, k: int) -> int:
    top = list(case.get("ranked_ids") or [])[:k]
    rel = set(case.get("relevant_ids") or [])
    return sum(1 for i in top if i in rel)


def hit_rate_at_k(cases: list[dict], k: int = 5) -> float:
    if not cases:
        return 0.0
    return sum(1 for c in cases if _hits(c, k) > 0) / len(cases)


def mrr_at_k(cases: list[dict], k: int = 10) -> float:
    if not cases:
        return 0.0
    total = 0.0
    for c in cases:
        rel = set(c.get("relevant_ids") or [])
        rr = 0.0
        for rank, rid in enumerate(list(c.get("ranked_ids") or [])[:k], 1):
            if rid in rel:
                rr = 1.0 / rank
                break
        total += rr
    return total / len(cases)


def precision_at_k(cases: list[dict], k: int = 5) -> float:
    if not cases:
        return 0.0
    return sum(_hits(c, k) for c in cases) / (len(cases) * k)


def recall_at_k(cases: list[dict], k: int = 5) -> float:
    if not cases:
        return 0.0
    vals = [
        _hits(c, k) / len(c["relevant_ids"])
        for c in cases
        if c.get("relevant_ids")
    ]
    return sum(vals) / len(cases) if vals else 0.0


def f1_at_k(cases: list[dict], k: int = 5) -> float:
    p, r = precision_at_k(cases, k), recall_at_k(cases, k)
    return 2 * p * r / (p + r) if (p + r) > 0 else 0.0


def evaluate(cases: list[dict], k: int = 5) -> dict:
    """聚合全部指标, 键名带 k 后缀(落 eval_runs.metrics / 监控页直用)。"""
    return {
        f"hit_rate_at_{k}": hit_rate_at_k(cases, k),
        f"mrr_at_{k}": mrr_at_k(cases, k),
        f"precision_at_{k}": precision_at_k(cases, k),
        f"recall_at_{k}": recall_at_k(cases, k),
        f"f1_at_{k}": f1_at_k(cases, k),
    }
