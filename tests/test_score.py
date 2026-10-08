"""composite_score 纯函数 — 零依赖直接断言(D10/D11)。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lawApp_LangGraph.score import BASELINES, composite_score  # noqa: E402

GOOD = {
    "rag_top_score": 0.9,
    "total_latency_ms": 20000,
    "token_prompt": 5000,
    "token_completion": 1500,
    "tool_count": 3,
    "tool_error_count": 0,
    "clarify_rounds": 0,
    "replan_rounds": 0,
}
BAD = {
    "rag_top_score": 0.1,
    "total_latency_ms": 120000,
    "token_prompt": 30000,
    "token_completion": 9000,
    "tool_count": 3,
    "tool_error_count": 2,
    "clarify_rounds": 3,
    "replan_rounds": 3,
}


def test_full_metrics_score_range():
    s = composite_score(GOOD)
    assert 0 <= s <= 100
    assert s >= 80  # 各分量全优 → 高分


def test_bad_run_scores_lower_than_good():
    assert composite_score(BAD) < composite_score(GOOD)


def test_missing_components_redistribute_weight():
    # 无检索分数(闲聊路径) → rag 权重重分配, 不报错不归零不满分
    m = {"total_latency_ms": 30000, "token_prompt": 100, "token_completion": 50,
         "tool_error_count": 0}
    s = composite_score(m)
    assert 0 < s <= 100
    # 时延/token 都远优于基线且无错误 → 应为高分
    assert s >= 90


def test_empty_metrics_zero():
    assert composite_score({}) == 0


def test_judge_slot_none_and_present():
    m = {"rag_top_score": 0.8, "total_latency_ms": 30000,
         "token_prompt": 6000, "token_completion": 2000}
    # judge=None: 只剩 rag 分量(其余缺数据重分配)
    without = composite_score(m, weights={"rag": 1.0})
    assert without == 80
    # judge=1.0: rag/judge 各半
    with_judge = composite_score(m, weights={"rag": 0.5, "judge": 0.5},
                                 judge_score=1.0)
    assert with_judge == 90


def test_baseline_relative_latency():
    # 基线公式: baseline/actual 再 clamp — 基线内=满分, 超基线线性衰减
    assert composite_score({"total_latency_ms": BASELINES["latency_ms"]},
                           weights={"latency": 1.0}) == 100
    assert composite_score({"total_latency_ms": BASELINES["latency_ms"] * 2},
                           weights={"latency": 1.0}) == 50
    assert composite_score({"total_latency_ms": BASELINES["latency_ms"] * 4},
                           weights={"latency": 1.0}) == 25
