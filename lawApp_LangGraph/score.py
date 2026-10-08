"""综合评分(D10) — 纯函数, 权重可调, judge 插槽(D11)。

规格: docs/superpowers/specs/2026-10-08-stage-chain-monitoring-design.md §三-2
分量: rag 检索质量 / 时延(相对基线) / token 成本(相对基线) / 工具正确率 /
      HITL 轮次 / replan 轮次; 分量缺数据 → 权重重分配, 不惩罚不报错。
LLM-as-judge(后续): judge_score 传入即生效, None 时权重重分配。
"""

from __future__ import annotations

from typing import Optional

DEFAULT_WEIGHTS = {
    "rag": 0.25,
    "latency": 0.15,
    "token": 0.15,
    "tool": 0.25,
    "hitl": 0.10,
    "replan": 0.10,
}
BASELINES = {"latency_ms": 60000.0, "token_total": 20000.0}
JUDGE = 0.15  # judge 接入时的默认权重(weights 未显式给 judge 时用)


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def composite_score(
    metrics: dict,
    weights: Optional[dict] = None,
    judge_score: Optional[float] = None,
) -> int:
    """0-100 综合分。分量缺数据 → 该分量权重在场分量间重分配。"""
    w = dict(weights or DEFAULT_WEIGHTS)
    comps: dict[str, float] = {}

    rag = metrics.get("rag_top_score")
    if rag is not None:
        comps["rag"] = _clamp(float(rag))

    lat = metrics.get("total_latency_ms")
    if lat:
        comps["latency"] = _clamp(BASELINES["latency_ms"] / max(float(lat), 1.0))

    tok = (metrics.get("token_prompt") or 0) + (metrics.get("token_completion") or 0)
    if tok:
        comps["token"] = _clamp(BASELINES["token_total"] / tok)

    tool_calls = metrics.get("tool_count") or 0
    tool_errs = metrics.get("tool_error_count") or 0
    if tool_calls:
        comps["tool"] = _clamp(1.0 - tool_errs / tool_calls)
    # 无工具调用(闲聊路径) → tool 分量缺数据走权重重分配, 不奖励不惩罚

    hitl = metrics.get("clarify_rounds") or 0
    if hitl:
        comps["hitl"] = _clamp(1.0 - hitl / 5.0)

    replan = metrics.get("replan_rounds") or 0
    if replan:
        comps["replan"] = _clamp(1.0 - replan / 5.0)

    if judge_score is not None:
        w.setdefault("judge", JUDGE)
        comps["judge"] = _clamp(float(judge_score))

    # 分量缺数据 → 权重重分配到在场分量
    active = {k: w[k] for k in comps if k in w and w[k] > 0}
    total_w = sum(active.values())
    if total_w == 0 or not active:
        return 0
    return int(round(100.0 * sum(comps[k] * weight for k, weight in active.items()) / total_w))
