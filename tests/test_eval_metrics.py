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
