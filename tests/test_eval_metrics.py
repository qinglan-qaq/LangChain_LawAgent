"""检索评测指标纯函数 — 含 P/R/F1 单 ground truth 退化路径(P2)。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lawApp_LangGraph.eval_metrics import (  # noqa: E402
    evaluate,
    f1_at_k,
    hit_rate_at_k,
    mrr_at_k,
    precision_at_k,
    recall_at_k,
)

CASES = [
    {"ranked_ids": ["A", "B", "C", "D", "E"], "relevant_ids": ["A"]},  # 命中@1
    {"ranked_ids": ["B", "C", "A", "D", "E"], "relevant_ids": ["A"]},  # 命中@3
    {"ranked_ids": ["B", "C", "D", "E", "F"], "relevant_ids": ["A"]},  # 未命中
]
MULTI = [{"ranked_ids": ["A", "B", "C", "D", "E"], "relevant_ids": ["A", "B"]}]


def test_hit_rate_at_5():
    assert hit_rate_at_k(CASES, k=5) == 2 / 3
    assert hit_rate_at_k(CASES, k=1) == 1 / 3


def test_mrr_at_10():
    # 1/1 + 1/3 + 0 = 4/3, 均值 4/9
    assert abs(mrr_at_k(CASES, k=10) - 4 / 9) < 1e-9


def test_prf_single_truth_degenerate():
    # 单 ground truth: precision@5 = 1/5(命中时), recall@5 = 1(命中时)
    assert precision_at_k(CASES[:1], k=5) == 1 / 5
    assert recall_at_k(CASES[:1], k=5) == 1.0
    assert abs(f1_at_k(CASES[:1], k=5) - 2 * (1 / 5 * 1) / (1 / 5 + 1)) < 1e-9


def test_prf_multi_truth_meaningful():
    assert precision_at_k(MULTI, k=5) == 2 / 5
    assert recall_at_k(MULTI, k=5) == 1.0


def test_evaluate_aggregates_all():
    out = evaluate(CASES, k=5)
    for key in ("hit_rate_at_5", "mrr_at_5", "precision_at_5",
                "recall_at_5", "f1_at_5"):
        assert key in out and 0.0 <= out[key] <= 1.0


def test_empty_cases_zero():
    assert evaluate([], k=5)["hit_rate_at_5"] == 0.0
    assert evaluate([], k=5)["mrr_at_5"] == 0.0


# ── RRF 改造: 标定辅助(纯函数) ──


def test_split_scores_by_relevance():
    """按 relevant_ids 把返回文档分成正/负两组(缺 relevance_score 跳过)。"""
    from lawApp_LangGraph.eval_metrics import split_scores

    docs = [
        {"id": "a", "relevance_score": 0.9},
        {"id": "b", "relevance_score": 0.2},
        {"id": "c", "rrf_score": 0.03},  # rerank 不可用 → 跳过
    ]
    pos, neg = split_scores(docs, ["a"])
    assert pos == [0.9] and neg == [0.2]


def test_suggest_thresholds_operating_points():
    """correct 线 = precision≥目标的最高切点; incorrect 线 = 下方 relevant<floor。"""
    from lawApp_LangGraph.eval_metrics import suggest_thresholds

    pos = [0.9, 0.85, 0.8]
    neg = [0.3, 0.1, 0.05]
    out = suggest_thresholds(pos, neg, precision_target=0.7, relevant_floor=0.05)
    assert out["correct_threshold"] == 0.9  # t=0.9: 1 正 0 负 → p=1.0
    assert out["incorrect_threshold"] == 0.05  # 最低切点即满足


def test_suggest_thresholds_insufficient_data():
    """空正例或空负例 → None 建议值(调用方沿用现值)。"""
    from lawApp_LangGraph.eval_metrics import suggest_thresholds

    out = suggest_thresholds([], [0.1])
    assert out["correct_threshold"] is None
    assert out["incorrect_threshold"] is None
    out2 = suggest_thresholds([0.9], [])
    assert out2["correct_threshold"] is None
