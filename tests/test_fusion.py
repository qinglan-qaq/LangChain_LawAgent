"""RRF 融合层单测 — 纯函数, 无 IO。"""

import math
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _m(mid, score=0.9, meta=None):
    """构造 Pinecone Match 形状的对象。"""
    return SimpleNamespace(id=mid, score=score, metadata=meta or {})


def test_sigmoid_range_and_rounding():
    """sigmoid: logits → [0,1], 保留 4 位小数。"""
    from lawApp_LangGraph.RAG_service.fusion import sigmoid

    assert sigmoid(0.0) == 0.5
    assert sigmoid(3.0) == round(1.0 / (1.0 + math.exp(-3.0)), 4)
    assert sigmoid(-3.0) == round(1.0 / (1.0 + math.exp(3.0)), 4)
    assert 0.0 <= sigmoid(-100.0) <= 1.0
    assert 0.0 <= sigmoid(100.0) <= 1.0


def test_rrf_fuse_overlap_both_routes():
    """双路同现文档: rrf = 两路 w/(k+rank) 之和, 双方名次都记录。"""
    from lawApp_LangGraph.RAG_service.fusion import rrf_fuse

    dense = [_m("a"), _m("b")]
    sparse = [_m("c"), _m("a")]
    out = rrf_fuse(dense, sparse, k=60)
    by_id = {c["id"]: c for c in out}
    assert by_id["a"]["rrf_score"] == round(1 / 61 + 1 / 62, 6)
    assert by_id["a"]["dense_rank"] == 1 and by_id["a"]["sparse_rank"] == 2
    assert by_id["b"]["sparse_rank"] is None
    assert by_id["c"]["dense_rank"] is None


def test_rrf_fuse_upper_bound_and_order():
    """双路 rank1 得满额 2/(k+1), 按 rrf 降序, rrf_rank 从 1 起。"""
    from lawApp_LangGraph.RAG_service.fusion import rrf_fuse

    dense = [_m("a"), _m("b"), _m("c")]
    sparse = [_m("a"), _m("d")]
    out = rrf_fuse(dense, sparse, k=60, top_n=10)
    assert out[0]["id"] == "a"
    assert out[0]["rrf_score"] == round(2 / 61, 6)
    assert out[0]["rrf_rank"] == 1
    # 降序不变式
    scores = [c["rrf_score"] for c in out]
    assert scores == sorted(scores, reverse=True)


def test_rrf_fuse_single_route_only():
    """单路独占: 另一路贡献 0 分, rank 为 None。"""
    from lawApp_LangGraph.RAG_service.fusion import rrf_fuse

    out = rrf_fuse([_m("a"), _m("b")], [], k=60)
    assert len(out) == 2
    assert out[0]["rrf_score"] == round(1 / 61, 6)
    assert all(c["sparse_rank"] is None for c in out)


def test_rrf_fuse_weights():
    """w_dense/w_sparse 权重生效。"""
    from lawApp_LangGraph.RAG_service.fusion import rrf_fuse

    out = rrf_fuse([_m("a")], [_m("a")], k=60, w_dense=2.0, w_sparse=0.5)
    assert out[0]["rrf_score"] == round(2 / 61 + 0.5 / 61, 6)


def test_rrf_fuse_top_n_cap():
    """top_n 截断候选集。"""
    from lawApp_LangGraph.RAG_service.fusion import rrf_fuse

    dense = [_m(f"d{i}") for i in range(10)]
    sparse = [_m(f"s{i}") for i in range(10)]
    assert len(rrf_fuse(dense, sparse, top_n=5)) == 5


def test_rrf_fuse_empty_inputs():
    """双空输入返回空列表; 仅稀疏路有值时照常融合(单路降级场景)。"""
    from lawApp_LangGraph.RAG_service.fusion import rrf_fuse

    assert rrf_fuse([], []) == []
    out = rrf_fuse([], [_m("a")], k=60)
    assert len(out) == 1 and out[0]["id"] == "a"


def test_rrf_fuse_dict_matches():
    """dict 形态召回项(测试替身)按 id 聚合, match 保留原始对象。"""
    from lawApp_LangGraph.RAG_service.fusion import rrf_fuse

    dense = [{"id": "x", "score": 0.9}]
    sparse = [{"id": "x", "score": 3.0}]
    out = rrf_fuse(dense, sparse, k=60)
    assert out[0]["id"] == "x"
    assert out[0]["match"] == dense[0]
