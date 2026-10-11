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


def split_scores(docs: list[dict], relevant_ids: list) -> tuple[list, list]:
    """把返回文档的 relevance_score 按 relevant_ids 分成正/负两组.

    Args:
        docs: retriever 返回的文档 dict 列表(含 id / relevance_score)。
        relevant_ids: golden set 的相关向量 id 列表。

    Returns:
        (正例分数列表, 负例分数列表); 缺 relevance_score 的文档
        (rerank 不可用)跳过, 不进任何一组。
    """
    rel = set(relevant_ids or [])
    pos, neg = [], []
    for d in docs or []:
        s = d.get("relevance_score")
        if s is None:
            continue
        (pos if d.get("id") in rel else neg).append(float(s))
    return pos, neg


def suggest_thresholds(
    pos_scores: list[float],
    neg_scores: list[float],
    precision_target: float = 0.7,
    relevant_floor: float = 0.05,
) -> dict:
    """从正/负分数分布建议 CRAG 三档切点(spec §5④ 运营点).

    Args:
        pos_scores: relevant_ids 内文档的 relevance_score 列表。
        neg_scores: relevant_ids 外文档的 relevance_score 列表。
        precision_target: correct 线的 precision 目标(默认 0.7, 引用安全线)。
        relevant_floor: incorrect 线下方允许的 relevant 占比(默认 0.05)。

    Returns:
        {"correct_threshold": 最高满足 precision≥目标的切点,
         "incorrect_threshold": 最低满足下方 relevant 占比<floor 的切点}。
        空正例或空负例(数据不足)时对应值为 None, 调用方沿用现值。
    """
    if not pos_scores or not neg_scores:
        return {"correct_threshold": None, "incorrect_threshold": None}

    cuts = sorted(set(pos_scores + neg_scores), reverse=True)

    correct_thr = None
    for t in cuts:  # 降序, 首个满足即最高切点(spec §5④)
        above_pos = sum(1 for s in pos_scores if s >= t)
        above_neg = sum(1 for s in neg_scores if s >= t)
        if (above_pos + above_neg) > 0 and (
            above_pos / (above_pos + above_neg) >= precision_target
        ):
            correct_thr = t
            break

    incorrect_thr = None
    for t in reversed(cuts):  # 升序找最低满足切点
        below_pos = sum(1 for s in pos_scores if s < t)
        below_all = sum(1 for s in pos_scores + neg_scores if s < t)
        if below_all == 0 or below_pos / below_all < relevant_floor:
            incorrect_thr = t  # 升序首中即最低切点
            break

    return {"correct_threshold": correct_thr,
            "incorrect_threshold": incorrect_thr}
