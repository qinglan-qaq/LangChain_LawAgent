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
