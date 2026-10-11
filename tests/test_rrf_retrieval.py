"""RRF 检索链路测试 — RAG_service 双路原语 / PineconeRetriever 编排 / 工具面语义。"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _m(mid, score=0.9, meta=None):
    """构造 Pinecone Match 形状的对象。"""
    return SimpleNamespace(id=mid, score=score, metadata=meta or {"year": "2023"})


def _run(coro):
    return asyncio.run(coro)


class _FakeIndex:
    """按调用形态分发稠密/稀疏结果的假 Pinecone 索引。"""

    def __init__(self, dense_matches, sparse_matches):
        self._dense = dense_matches
        self._sparse = sparse_matches
        self.calls = []

    def query(self, vector=None, sparse_vector=None, namespace=None,
              top_k=None, include_metadata=False):
        self.calls.append(
            {"vector": vector, "sparse_vector": sparse_vector, "top_k": top_k}
        )
        matches = self._dense if vector is not None else self._sparse
        return SimpleNamespace(matches=list(matches)[: top_k or 10])


def _make_svc(dense, sparse):
    """构造绕过模型冷启动的 RAG_service 实例(假索引+假嵌入+假BM25)。"""
    from lawApp_LangGraph.RAG_service.RAG_program import RAG_service

    svc = RAG_service.__new__(RAG_service)
    svc.index = _FakeIndex(dense, sparse)
    svc.embeddings = SimpleNamespace(embed_query=lambda q: [0.1] * 8)
    svc.bm25 = SimpleNamespace(
        encode_queries=lambda q: {"indices": [1, 2], "values": [0.5, 0.5]}
    )
    return svc


def test_dense_and_sparse_recall_split_queries():
    """dense_recall/sparse_recall 各自只发本路查询, 返回 Match 列表。"""
    dense = [_m("a"), _m("b")]
    sparse = [_m("a"), _m("c")]
    svc = _make_svc(dense, sparse)

    d = svc.dense_recall("查询", "ns", top_k=5)
    s = svc.sparse_recall("查询", "ns", top_k=5)
    assert [x.id for x in d] == ["a", "b"]
    assert [x.id for x in s] == ["a", "c"]
    # 两次调用 = 一次纯向量 + 一次纯稀疏, 无凸组合
    assert svc.index.calls[0]["vector"] is not None
    assert svc.index.calls[0]["sparse_vector"] is None
    assert svc.index.calls[1]["vector"] is None
    assert svc.index.calls[1]["sparse_vector"] is not None


def test_recall_requires_initialized_index():
    """索引未初始化 → RuntimeError 中文文案(保留旧防御)。"""
    from lawApp_LangGraph.RAG_service.RAG_program import RAG_service

    svc = RAG_service.__new__(RAG_service)
    svc.index = None
    for fn in (svc.dense_recall, svc.sparse_recall):
        try:
            fn("q", "ns", 5)
            raise AssertionError("应当 RuntimeError")
        except RuntimeError as e:
            assert "索引未初始化" in str(e)


def test_convex_path_fully_removed():
    """凸组合路径全仓清除: search_withDenseSparse / hybrid_convex_scale 不存在。"""
    import inspect

    from lawApp_LangGraph.RAG_service import RAG_program

    src = inspect.getsource(RAG_program)
    assert not hasattr(RAG_program.RAG_service, "search_withDenseSparse")
    assert "hybrid_convex_scale" not in src
    assert "CrossEncoder" not in src  # __init__ 重排器一并移除


# ── PineconeRetriever 编排(gather 双路 → RRF → rerank → sigmoid 出分) ──


def _boom():
    raise RuntimeError("dense route down")


def _patch_pinecone(monkeypatch, dense, sparse, rerank_scores=None,
                    dense_error=False):
    """装配假后端: _get_service → 假 RAG_service; embedder.rerank 可控。"""
    from lawApp_LangGraph.RAG_service import embedder
    from lawApp_LangGraph.RAG_service import pinecone_retriever as pr

    svc = SimpleNamespace(
        dense_recall=lambda q, ns, k: (_boom() if dense_error else dense[:k]),
        sparse_recall=lambda q, ns, k: sparse[:k],
    )
    monkeypatch.setattr(pr, "_get_service", lambda: svc)

    async def _fake_rerank(query, docs, text_key="chunk_text"):
        return rerank_scores

    monkeypatch.setattr(embedder, "rerank", _fake_rerank)
    return pr


def test_retriever_full_pipeline_rerank_scores(monkeypatch):
    """双路融合 → rerank → sigmoid: relevance/hybrid 别名 + 排序按重排分。"""
    from lawApp_LangGraph.RAG_service.fusion import sigmoid

    dense = [_m("a", meta={"chunk_text": "甲", "year": "2023"}),
             _m("b", meta={"chunk_text": "乙", "year": "2022"})]
    sparse = [_m("a", meta={"chunk_text": "甲", "year": "2023"}),
              _m("c", meta={"chunk_text": "丙", "year": "2021"})]
    pr = _patch_pinecone(monkeypatch, dense, sparse,
                         rerank_scores=[0.5, 3.0, -1.0])  # a,b,c 的 logits

    out = _run(pr.PineconeRetriever().search("查询", top_k=10, rerank_top_n=2))
    assert len(out) == 2
    # rerank logits 3.0(b) > 0.5(a) → b 第一
    assert out[0]["id"] == "b" and out[0]["rank"] == 1
    assert out[0]["relevance_score"] == sigmoid(3.0)
    assert out[0]["hybrid_score"] == out[0]["relevance_score"]
    assert out[0]["rrf_score"] > 0
    assert "chunk_text" in out[0] and len(out[0]["chunk_text"]) <= 500


def test_retriever_rerank_unavailable_degrade(monkeypatch):
    """rerank 返回 None → 文档不携带 relevance_score, 排序退 RRF 序。"""
    dense = [_m("a"), _m("b")]
    sparse = [_m("a"), _m("c")]
    pr = _patch_pinecone(monkeypatch, dense, sparse, rerank_scores=None)

    out = _run(pr.PineconeRetriever().search("查询", top_k=10, rerank_top_n=2))
    assert len(out) == 2
    # a 双路 rank1 → RRF 第一
    assert out[0]["id"] == "a" and out[0]["rank"] == 1
    assert all("relevance_score" not in d for d in out)
    assert all("hybrid_score" not in d for d in out)
    assert all("rrf_score" in d for d in out)


def test_retriever_single_route_degrades_to_sparse(monkeypatch):
    """稠密路失败 → 稀疏单路降级不抛错。"""
    dense = [_m("a"), _m("b")]
    pr = _patch_pinecone(monkeypatch, dense, [_m("c")], rerank_scores=None,
                         dense_error=True)

    out = _run(pr.PineconeRetriever().search("查询", top_k=10, rerank_top_n=3))
    assert [d["id"] for d in out] == ["c"]


def test_base_signature_alpha_removed():
    """BaseRetriever.search / 两个后端 search 签名均无 alpha。"""
    import inspect

    from lawApp_LangGraph.RAG_service.base import BaseRetriever
    from lawApp_LangGraph.RAG_service.pinecone_retriever import PineconeRetriever
    from lawApp_LangGraph.RAG_service.pgvector_retriever import PgvectorRetriever

    for fn in (BaseRetriever.search, PgvectorRetriever.search,
               PineconeRetriever.search):
        assert "alpha" not in inspect.signature(fn).parameters


# ── 工具面: retrieve 透传新字段 / evaluate 改判 + 降级门控 ──


def test_retrieve_tool_signature_and_passthrough(monkeypatch):
    """工具签名无 alpha; 假后端文档原样透传 + status=success。"""
    import inspect

    from lawApp_LangGraph.RAG_service import base as rag_base
    from lawApp_LangGraph.tools.rag_tools import retrieve_legal_knowledge

    # async 工具的 func 为 None, 形参经 .args 反射
    assert "alpha" not in retrieve_legal_knowledge.args
    assert "rrf" in retrieve_legal_knowledge.description.lower()

    class _Fake:
        async def search(self, query, top_k=20, rerank_top_n=5,
                         namespace=None):
            return [{"id": "a", "chunk_text": "x", "rrf_score": 0.03,
                     "relevance_score": 0.9, "hybrid_score": 0.9, "rank": 1}]

    monkeypatch.setattr(rag_base, "get_retriever", lambda: _Fake())
    out = _run(retrieve_legal_knowledge.ainvoke({"query": "测试"}))
    assert out["status"] == "success" and out["count"] == 1
    assert out["rag_documents"][0]["relevance_score"] == 0.9


def test_evaluate_tiers_on_relevance_score(monkeypatch):
    """三档判 relevance_score(不再看召回分)。"""
    from lawApp_LangGraph.config import settings
    from lawApp_LangGraph.tools.rag_tools import evaluate_case_relevance

    monkeypatch.setattr(settings, "correct_threshold", 0.7)
    monkeypatch.setattr(settings, "incorrect_threshold", 0.3)
    monkeypatch.setattr(settings, "min_quality_docs", 1)

    docs = [
        {"id": "a", "relevance_score": 0.95, "hybrid_score": 0.1,
         "chunk_text": "高相关", "case_number": "n1", "year": "2023"},
        {"id": "b", "relevance_score": 0.5, "hybrid_score": 0.9,
         "chunk_text": "中相关", "case_number": "n2", "year": "2022"},
        {"id": "c", "relevance_score": 0.1, "hybrid_score": 0.8,
         "chunk_text": "低相关", "case_number": "n3", "year": "2021"},
    ]
    out = evaluate_case_relevance.invoke({"documents": docs})
    ev = out["evaluation"]
    assert ev.correct_count == 1 and ev.ambiguous_count == 1
    assert ev.incorrect_count == 1
    # hybrid_score 高但 relevance 低 → 不进 correct(b/c 反证旧判据已废)
    assert [d.id for d in ev.correct] == ["a"]
    assert ev.quality_verdict == "充足"


def test_evaluate_degrades_all_ambiguous_when_rerank_missing():
    """文档缺 relevance_score(rerank 不可用)→ 全 ambiguous + verdict 强制不足。"""
    from lawApp_LangGraph.tools.rag_tools import evaluate_case_relevance

    # 即使阈值宽松、数量充足也必须判不足 — 门控不骗人
    docs = [
        {"id": "a", "rrf_score": 0.03, "chunk_text": "x",
         "case_number": "n1", "year": "2023"},
    ] * 5
    out = evaluate_case_relevance.invoke({"documents": docs})
    ev = out["evaluation"]
    assert ev.correct_count == 0 and ev.incorrect_count == 0
    assert ev.ambiguous_count == 5
    assert ev.quality_verdict == "不足,建议进行网络搜索补充"
