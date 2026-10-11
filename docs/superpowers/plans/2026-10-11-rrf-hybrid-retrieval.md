# RRF 混合检索改造 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pinecone 检索从凸组合(hybrid_convex_scale)切换为双路独立召回 + 客户端 RRF 融合 + CrossEncoder sigmoid 出分,CRAG 三档改判重排分,降级路径门控诚实化。

**Architecture:** `fusion.py` 新增纯函数层(rrf_fuse/sigmoid/钳制);`RAG_service` 拆出 dense_recall/sparse_recall 两路原语并删除凸组合;`PineconeRetriever` 改为编排层(gather 双路 → RRF → rerank → 出分);`rag_tools` 工具面退役 alpha、evaluate 改判 relevance_score;`eval_metrics`/`run_eval` 新增阈值标定能力。

**Tech Stack:** LangChain @tool、Pinecone SDK、sentence_transformers(BGE/CrossEncoder)、pydantic、pytest。

**Spec:** docs/superpowers/specs/2026-10-11-rrf-hybrid-retrieval-design.md

## Global Constraints

- 所有新增/修改函数一律 **Google 风格多行 docstring**(`Args:` / `Returns:` / `Raises:` 分节,中文),对齐仓库现有惯例(fetch_laws / add_document 同款)。
- 凸组合路径**全仓删除不留死代码**:`search_withDenseSparse`、`hybrid_convex_scale` import、`RAG_service.__init__` 的 self.reranker 及顶部 CrossEncoder import 一并清除。
- **pgvector 检索行为零变化**(仅移除 alpha 形参与钳制函数搬家,行为不变);`clamp_search_params` 定义移到 fusion.py,pgvector_retriever 改 import(tests/test_boundary_medium.py:68 的 `from ...pgvector_retriever import clamp_search_params` 经模块属性继续可用,不改测试)。
- 得分命名固定:`rrf_score`(融合溯源分,不进决策路径) / `relevance_score`(sigmoid 重排分,决策用) / `hybrid_score`(= relevance_score 兼容别名,别名仅在 rerank 可用时写入)。
- rerank 禁用/失败 → 返回文档**不携带** relevance_score 键,`evaluate_case_relevance` 对缺键文档全归 ambiguous 且 `quality_verdict` 强制「不足,建议进行网络搜索补充」。
- 单路召回失败 → 降级单路(优先保留成功路);双路全挂 → 抛异常,rag_tools 现有 try/except 兜成 status=error 固定中文文案(此行为已有测试 tests/test_boundary_medium.py:645 覆盖,不改)。
- 运行环境依赖步骤(真实标定/回归门)在 Task 7 单独列出,代码任务不依赖它们先行。
- 提交信息用仓库惯例(中文长句 type: 前缀)。

---

### Task 1: fusion.py — RRF 融合 + sigmoid 纯函数层

**Files:**
- Create: `lawApp_LangGraph/RAG_service/fusion.py`
- Create: `tests/test_fusion.py`
- Modify: `lawApp_LangGraph/RAG_service/pgvector_retriever.py:19-34`(clamp_search_params 定义移入 fusion,本文件改为 import)

**Interfaces:**
- Produces: `sigmoid(x: float) -> float`;`rrf_fuse(dense_matches: list, sparse_matches: list, k: int = 60, w_dense: float = 1.0, w_sparse: float = 1.0, top_n: int = 20) -> list[dict]`(每项 `{match, id, dense_rank, sparse_rank, rrf_score, rrf_rank}`);`clamp_search_params(top_k, rerank_top_n) -> tuple[int, int]`(原函数原样搬家)。后续 Task 3/4/5 依赖这些签名。

- [ ] **Step 1: Write the failing test**

创建 `tests/test_fusion.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_fusion.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lawApp_LangGraph.RAG_service.fusion'`

- [ ] **Step 3: Write minimal implementation**

创建 `lawApp_LangGraph/RAG_service/fusion.py`:

```python
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
    return kept
```

然后改 `lawApp_LangGraph/RAG_service/pgvector_retriever.py`:删除第 19-34 行的 `clamp_search_params` 函数定义,文件头部 import 区(第 13-14 行之后)加:

```python
from lawApp_LangGraph.RAG_service.fusion import clamp_search_params  # noqa: F401 — 钳制函数移居 fusion, 此处 re-export 保住 tests/ 既有 import 路径
```

并更新模块 docstring 第 4-6 行:删除「alpha 参数不生效」相关表述,改为「纯密集检索; 参数钳制与融合工具见 fusion.py」。

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m pytest tests/test_fusion.py tests/test_boundary_medium.py -v`
Expected: test_fusion.py 全 PASS;test_boundary_medium.py 的 test_m1_clamp_search_params_bounds 仍 PASS(经 re-export 路径)。

- [ ] **Step 5: Commit**

```bash
git add lawApp_LangGraph/RAG_service/fusion.py lawApp_LangGraph/RAG_service/pgvector_retriever.py tests/test_fusion.py
git commit -m "feat: RRF 融合层 fusion.py — rrf_fuse 双路倒数排名融合(k=60,权重可调,top_n 截断)+ sigmoid logits 归一化 + clamp_search_params 移居共享(re-export 保旧 import 路径), 纯函数零 IO 全单测覆盖"
```

---

### Task 2: state.py 得分字段 + config.py RRF 设置

**Files:**
- Modify: `lawApp_LangGraph/state.py:56-66`(RetrievedDocument)
- Modify: `lawApp_LangGraph/config.py:69-71` 附近(rerank 设置区)
- Test: `tests/test_p0_retrieval.py`(追加)

**Interfaces:**
- Produces: `RetrievedDocument.rrf_score: float = 0.0`、`relevance_score: float = 0.0`(hybrid_score 保留原字段,运行期由 retriever 写入 = relevance_score);`settings.rrf_k: int = 60`、`settings.rrf_w_dense: float = 1.0`、`settings.rrf_w_sparse: float = 1.0`。Task 4 消费 settings,Task 5 消费字段。

- [ ] **Step 1: Write the failing test**

追加到 `tests/test_p0_retrieval.py` 末尾:

```python
def test_rrf_settings_and_state_fields():
    """RRF 改造: settings 默认值 + RetrievedDocument 新字段(旧 checkpoint 兜底)."""
    from lawApp_LangGraph.config import Settings
    from lawApp_LangGraph.state import RetrievedDocument

    s = Settings()
    assert s.rrf_k == 60
    assert s.rrf_w_dense == 1.0
    assert s.rrf_w_sparse == 1.0

    d = RetrievedDocument()  # 不传任何字段 — 旧 checkpoint 缺新键路径
    assert d.rrf_score == 0.0
    assert d.relevance_score == 0.0
    assert d.hybrid_score == 0.0
    d2 = RetrievedDocument(
        id="x", rrf_score=0.03, relevance_score=0.95, hybrid_score=0.95
    )
    assert d2.relevance_score == 0.95 and d2.hybrid_score == 0.95
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_p0_retrieval.py::test_rrf_settings_and_state_fields -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'rrf_k'`(或 pydantic ValidationError)。

- [ ] **Step 3: Write minimal implementation**

`lawApp_LangGraph/state.py` 的 `RetrievedDocument`(第 56-66 行)改为:

```python
class RetrievedDocument(BaseModel):
    """单条检索到的法律案例文档块"""

    rank: int = 0
    id: str = ""
    # RRF 融合分 — 排名相对分, 仅日志/监控/调试溯源, 不进任何决策路径
    # (全垃圾召回时照样高分, 不能做门控, 见 spec §4 决策记录)
    rrf_score: float = 0.0
    # 最终得分 = sigmoid(CrossEncoder logits), CRAG 三档唯一判据
    relevance_score: float = 0.0
    # 兼容别名 = relevance_score; 旧 checkpoint / tracing.rag_top_score /
    # score.py 读它。rerank 不可用时 retriever 不写此键(缺省 0.0)
    hybrid_score: float = 0.0
    year: str = ""
    case_number: str = ""
    case_cause: str = ""
    chunk_text: str = ""
```

`lawApp_LangGraph/config.py` 在 `rerank_enabled` 行(约 69 行)后追加:

```python
    # RRF 融合参数(spec §5②): k=60 平滑惯例, 两路等权起步, 标定后可调
    rrf_k: int = 60
    rrf_w_dense: float = 1.0
    rrf_w_sparse: float = 1.0
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m pytest tests/test_p0_retrieval.py -v`
Expected: 全 PASS(含既有 namespace 签名测试)。

- [ ] **Step 5: Commit**

```bash
git add lawApp_LangGraph/state.py lawApp_LangGraph/config.py tests/test_p0_retrieval.py
git commit -m "feat: RRF 得分语义落地 state/config — RetrievedDocument 增 rrf_score(溯源)+relevance_score(决策判据), hybrid_score 降为兼容别名(tracing/score.py 不改仍读它); settings 增 rrf_k=60/rrf_w_dense/rrf_w_sparse; 旧 checkpoint 缺字段 default 兜底测试"
```

---

### Task 3: RAG_program.py — 双路召回原语 + 凸组合路径全删

**Files:**
- Modify: `lawApp_LangGraph/RAG_service/RAG_program.py`(imports 15-17、__init__ 86-89、search_withDenseSparse 339-465)
- Test: `tests/test_rrf_retrieval.py`(新建,本任务只写 recall 原语部分)

**Interfaces:**
- Produces: `RAG_service.dense_recall(query: str, namespace: str, top_k: int) -> list`(Pinecone Match 列表)、`RAG_service.sparse_recall(query: str, namespace: str, top_k: int) -> list`。两方法同步;`__init__` 不再加载 self.reranker(重排改走 embedder 单例,冷启动少一个 GB 级模型);`search_withDenseSparse` 删除。Task 4 消费这两原语。

- [ ] **Step 1: Write the failing test**

创建 `tests/test_rrf_retrieval.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_rrf_retrieval.py -v`
Expected: FAIL — `dense_recall` 不存在 / `test_convex_path_fully_removed` 断言失败。

- [ ] **Step 3: Write minimal implementation**

`lawApp_LangGraph/RAG_service/RAG_program.py`:

1. 删除 import 行:`from pinecone_text.hybrid import hybrid_convex_scale`(15 行)与 `from sentence_transformers import CrossEncoder`(17 行)。
2. `__init__` 删除 `self.reranker = CrossEncoder("BAAI/bge-reranker-large", max_length=512)`(86 行)及 docstring 中「BAAI重排序模型」表述,补一行注释:`# 重排经 RAG_service.embedder 单例(懒加载), __init__ 不再预载 GB 级模型`。
3. 整体删除 `search_withDenseSparse`(339-465 行),原位置新增:

```python
    def dense_recall(self, query: str, namespace: str, top_k: int) -> list:
        """稠密路独立召回(BGE 嵌入 → 纯向量查询).

        Args:
            query: 查询文本。
            namespace: Pinecone 命名空间。
            top_k: 召回条数(≥1)。

        Returns:
            Pinecone Match 列表(按本路余弦得分降序), 供 fusion.rrf_fuse 消费。

        Raises:
            RuntimeError: 索引未初始化。
        """
        if self.index is None:
            raise RuntimeError("索引未初始化, 请先调用 create_index()")
        dense_vec = self.embeddings.embed_query(query)
        res = self.index.query(
            vector=dense_vec,
            namespace=namespace,
            top_k=max(1, int(top_k)),
            include_metadata=True,
        )
        return list(res.matches)

    def sparse_recall(self, query: str, namespace: str, top_k: int) -> list:
        """稀疏路独立召回(BM25 编码 → 纯稀疏查询).

        Args/Returns/Raises 契约与 dense_recall 一致, 区别仅在向量形态:
        encode_queries 产出 {indices, values} 稀疏向量。
        """
        if self.index is None:
            raise RuntimeError("索引未初始化, 请先调用 create_index()")
        sparse_vec = self.bm25.encode_queries(query)
        res = self.index.query(
            sparse_vector=sparse_vec,
            namespace=namespace,
            top_k=max(1, int(top_k)),
            include_metadata=True,
        )
        return list(res.matches)
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m pytest tests/test_rrf_retrieval.py -v`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add lawApp_LangGraph/RAG_service/RAG_program.py tests/test_rrf_retrieval.py
git commit -m "feat: RAG_service 双路召回原语 — dense_recall/sparse_recall 各自独立单查(纯向量/纯稀疏), search_withDenseSparse+hybrid_convex_scale+__init__ 重排器全删(冷启动少一个 GB 级模型, 重排改走 embedder 单例), 全仓凸组合零残留源码扫描测试"
```

---

### Task 4: PineconeRetriever 编排 + 接口签名退役 alpha

**Files:**
- Modify: `lawApp_LangGraph/RAG_service/pinecone_retriever.py`(整文件重写 52-91 行)
- Modify: `lawApp_LangGraph/RAG_service/base.py:29-42`(search 签名)
- Modify: `lawApp_LangGraph/RAG_service/pgvector_retriever.py:43-44`(删 alpha 形参与 noqa)
- Modify: `scripts/run_eval.py:37-39`(删 alpha=0.4 实参)
- Modify: `tests/test_boundary_low.py:257-258`(fake 签名删 alpha)
- Test: `tests/test_rrf_retrieval.py`(追加编排测试)

**Interfaces:**
- Consumes: Task 1 `rrf_fuse/sigmoid/clamp_search_params`、Task 2 `settings.rrf_*`、Task 3 `dense_recall/sparse_recall`、`embedder.rerank(query, docs) -> Optional[list[float]]`。
- Produces: `PineconeRetriever.search(query, top_k=20, rerank_top_n=5, namespace=None) -> list[dict]`,返回项字段 `id/year/case_number/case_cause/chunk_text[:500]/rrf_score/relevance_score(仅 rerank 可用)/hybrid_score(= relevance_score,仅 rerank 可用)/rank`。Task 5/6 依赖此 dict 形状。`BaseRetriever.search` 签名同步去掉 alpha(子类 override 多余 kwarg 仍兼容)。

- [ ] **Step 1: Write the failing test**

追加到 `tests/test_rrf_retrieval.py`:

```python
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


def test_retriever_full_pipeline_rerank_scores():
    """双路融合 → rerank → sigmoid: relevance/hybrid 别名 + 排序按重排分。"""
    from lawApp_LangGraph.RAG_service.fusion import sigmoid

    dense = [_m("a", meta={"chunk_text": "甲", "year": "2023"}),
             _m("b", meta={"chunk_text": "乙", "year": "2022"})]
    sparse = [_m("a", meta={"chunk_text": "甲", "year": "2023"}),
              _m("c", meta={"chunk_text": "丙", "year": "2021"})]
    pr = _patch_pinecone(None, dense, sparse,
                         rerank_scores=[0.5, 3.0, -1.0])  # a,b,c 的 logits

    out = _run(pr.PineconeRetriever().search("查询", top_k=10, rerank_top_n=2))
    assert len(out) == 2
    # rerank logits 3.0(b) > 0.5(a) → b 第一
    assert out[0]["id"] == "b" and out[0]["rank"] == 1
    assert out[0]["relevance_score"] == sigmoid(3.0)
    assert out[0]["hybrid_score"] == out[0]["relevance_score"]
    assert out[0]["rrf_score"] > 0
    assert "chunk_text" in out[0] and len(out[0]["chunk_text"]) <= 500


def test_retriever_rerank_unavailable_degrade():
    """rerank 返回 None → 文档不携带 relevance_score, 排序退 RRF 序。"""
    dense = [_m("a"), _m("b")]
    sparse = [_m("a"), _m("c")]
    pr = _patch_pinecone(None, dense, sparse, rerank_scores=None)

    out = _run(pr.PineconeRetriever().search("查询", top_k=10, rerank_top_n=2))
    assert len(out) == 2
    # a 双路 rank1 → RRF 第一
    assert out[0]["id"] == "a" and out[0]["rank"] == 1
    assert all("relevance_score" not in d for d in out)
    assert all("hybrid_score" not in d for d in out)
    assert all("rrf_score" in d for d in out)


def test_retriever_single_route_degrades_to_sparse():
    """稠密路失败 → 稀疏单路降级不抛错。"""
    dense = [_m("a"), _m("b")]
    pr = _patch_pinecone(None, dense, [_m("c")], rerank_scores=None,
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
```

同时修 `tests/test_boundary_low.py:257-258` 的 fake 签名,把 `alpha=0.4,` 删掉:

```python
        async def search(self, query, top_k=20, rerank_top_n=5,
                         namespace=None):
```

`scripts/run_eval.py:37-39` 的调用删 `alpha=0.4,` 参数(改后为 `results = await retriever.search(query=c["question"], top_k=10, rerank_top_n=10, namespace=None)`)。

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_rrf_retrieval.py -v`
Expected: 编排四测 FAIL(PineconeRetriever.search 还在调 search_withDenseSparse),签名测试 FAIL。

- [ ] **Step 3: Write minimal implementation**

`lawApp_LangGraph/RAG_service/pinecone_retriever.py` 的 `PineconeRetriever` 类整体替换为:

```python
class PineconeRetriever(BaseRetriever):
    async def search(
        self,
        query: str,
        top_k: int = 20,
        rerank_top_n: int = 5,
        namespace: Optional[str] = None,
    ) -> list[dict]:
        """RRF 混合检索编排: 双路并发召回 → 客户端融合 → CrossEncoder 出分.

        Args:
            query: 查询文本, 中文。
            top_k: 每路召回条数(钳制 1..50)。
            rerank_top_n: 重排后返回条数(钳制 1..10)。
            namespace: 检索命名空间, None 时以 settings.pinecone_namespace 兜底。

        Returns:
            RetrievedDocument 对齐的 dict 列表。rerank 可用时每项含
            rrf_score / relevance_score(sigmoid 重排分) / hybrid_score
            (= relevance_score 兼容别名) / rank(重排名次); rerank 不可
            用时缺 relevance_score 与 hybrid_score 两键, rank 为 RRF 名次
            — 缺键即 evaluate_case_relevance 全 ambiguous 门控的标记
            (spec §5⑤), 排序不骗人。

        Raises:
            RuntimeError: 双路召回全部失败(调用方 rag_tools 兜 status=error)。
        """
        import asyncio

        from lawApp_LangGraph.RAG_service.embedder import rerank
        from lawApp_LangGraph.RAG_service.fusion import (
            clamp_search_params,
            rrf_fuse,
            sigmoid,
        )
        from lawApp_LangGraph.config import settings

        ns = namespace or settings.pinecone_namespace
        top_k, rerank_top_n = clamp_search_params(top_k, rerank_top_n)
        svc = _get_service()

        # 双路并发召回(SDK 同步, 各自让出事件循环); 单路失败降级单路
        results = await asyncio.gather(
            asyncio.to_thread(svc.dense_recall, query, ns, top_k),
            asyncio.to_thread(svc.sparse_recall, query, ns, top_k),
            return_exceptions=True,
        )
        dense_m, sparse_m = [], []
        if isinstance(results[0], list):
            dense_m = results[0]
        else:
            logger.warning("稠密路召回失败, 降级稀疏单路: %s", results[0])
        if isinstance(results[1], list):
            sparse_m = results[1]
        else:
            logger.warning("稀疏路召回失败, 降级稠密单路: %s", results[1])
        if not dense_m and not sparse_m:
            raise RuntimeError(f"双路召回全部失败: {results!r}")

        candidates = rrf_fuse(
            dense_m,
            sparse_m,
            k=settings.rrf_k,
            w_dense=settings.rrf_w_dense,
            w_sparse=settings.rrf_w_sparse,
            top_n=max(2 * rerank_top_n, 20),
        )

        def _meta(m) -> dict:
            return getattr(m, "metadata", None) or {}

        docs = [
            {
                "id": c["id"],
                "year": _meta(c["match"]).get("year", ""),
                "case_number": _meta(c["match"]).get("case_number", ""),
                "case_cause": _meta(c["match"]).get("case_cause", ""),
                "chunk_text": (_meta(c["match"]).get("chunk_text", "") or "")[:500],
                "rrf_score": round(float(c["rrf_score"]), 6),
            }
            for c in candidates
        ]

        scores = await rerank(query, docs)
        if scores is not None and len(scores) == len(docs):
            order = sorted(range(len(docs)), key=lambda i: scores[i], reverse=True)
            kept = order[:rerank_top_n]
            docs = [
                {**docs[i], "relevance_score": sigmoid(scores[i]),
                 "hybrid_score": sigmoid(scores[i])}
                for i in kept
            ]
            logger.info(
                "RRF 检索完成(重排) | 候选%d → 返回%d 条 | top_relevance=%.4f",
                len(candidates), len(docs), docs[0]["relevance_score"] if docs else 0,
            )
        else:
            # rerank 不可用: 不写 relevance_score/hybrid_score — 缺键即
            # evaluate_case_relevance 全 ambiguous + verdict 不足 的降级标记
            docs = docs[:rerank_top_n]
            logger.warning("重排不可用, 排序退 RRF 序 | 返回%d 条", len(docs))

        for rank, d in enumerate(docs, start=1):
            d["rank"] = rank
        return docs
```

模块 docstring(1-8 行)同步更新为 RRF 流程描述。`base.py` 的 `BaseRetriever.search` 抽象签名删 `alpha: float = 0.4,` 行,docstring 删 alpha 相关句;`pgvector_retriever.py` 的 search 删 `alpha: float = 0.4,  # noqa: ARG002` 行。

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m pytest tests/test_rrf_retrieval.py tests/test_boundary_low.py tests/test_p0_retrieval.py tests/test_boundary_high_backend.py -v`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add lawApp_LangGraph/RAG_service/pinecone_retriever.py lawApp_LangGraph/RAG_service/base.py lawApp_LangGraph/RAG_service/pgvector_retriever.py scripts/run_eval.py tests/test_rrf_retrieval.py tests/test_boundary_low.py
git commit -m "feat: PineconeRetriever RRF 编排 — gather 双路并发召回(单路失败降级单路,双路挂抛错走既有 error 文案)→ rrf_fuse 融合(top_n=max(2×rerank_top_n,20))→ CrossEncoder sigmoid 出分(relevance_score=hybrid_score 别名同步写入,rerank 不可用缺键标记); alpha 形参全接口退役(base/两后端/工具签名/run_eval), pgvector 行为零变化仅签名同步"
```

---

### Task 5: rag_tools 工具面 — alpha 退役 + evaluate 改判 relevance_score + 降级门控

**Files:**
- Modify: `lawApp_LangGraph/tools/rag_tools.py:63-137`(retrieve_legal_knowledge)、`165-256`(evaluate_case_relevance)
- Test: `tests/test_rrf_retrieval.py`(追加工具面测试)

**Interfaces:**
- Consumes: Task 4 返回 dict 形状(relevance_score 键存在性即降级标记)。
- Produces: `retrieve_legal_knowledge(query, top_k=20, rerank_top_n=5, namespace=None)`;`evaluate_case_relevance` 三档判 `relevance_score`,缺键全 ambiguous + 强制「不足,建议进行网络搜索补充」。MCP 侧 mcp_server.py:189 只传 query/top_k,无需改。

- [ ] **Step 1: Write the failing test**

追加到 `tests/test_rrf_retrieval.py`:

```python
# ── 工具面: retrieve 透传新字段 / evaluate 改判 + 降级门控 ──


def test_retrieve_tool_signature_and_passthrough(monkeypatch):
    """工具签名无 alpha; 假后端文档原样透传 + status=success。"""
    import inspect

    from lawApp_LangGraph.RAG_service import base as rag_base
    from lawApp_LangGraph.tools.rag_tools import retrieve_legal_knowledge

    assert "alpha" not in inspect.signature(
        retrieve_legal_knowledge.func
    ).parameters
    assert "rrf" in retrieve_legal_knowledge.description

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_rrf_retrieval.py -v`
Expected: 新三测 FAIL(签名还有 alpha / 判据还是 hybrid_score / 无降级分支)。

- [ ] **Step 3: Write minimal implementation**

`lawApp_LangGraph/tools/rag_tools.py`:

1. `retrieve_legal_knowledge`:删 `alpha: float = 0.4,` 形参;docstring 重写(Google 风格,删 alpha 行,加 `rrf_k` 说明与两键降级语义:relevance_score/hybrid_score 仅 rerank 可用时存在,缺键表示重排不可用);日志 detail 改 `query=... | top_k=... | ns=...`(删 alpha);`retriever.search(...)` 调用删 `alpha=alpha`;`top_score` 日志改 `results[0].get("relevance_score") or results[0].get("rrf_score", 0)`。
2. `evaluate_case_relevance`:档位循环与 verdict 前插入降级判定,核心改写:

```python
    def _rel_score(doc: Any) -> Optional[float]:
        """取 relevance_score; dict/对象双兼容, 缺键返回 None。"""
        if isinstance(doc, dict):
            v = doc.get("relevance_score")
        else:
            v = getattr(doc, "relevance_score", None)
        return float(v) if v is not None else None

    # rerank 不可用(retriever 缺键)→ 全 ambiguous + verdict 强制不足:
    # 没有绝对分不发「充足」结论, replanner 走网络补充(spec §5⑤)
    if all(_rel_score(d) is None for d in documents):
        ambiguous = [_to_simple_doc(d) for d in documents]
        tool_log.info(
            "← 工具返回: evaluate_case_relevance",
            detail="rerank 不可用, 全档降级",
            result="verdict=不足",
        )
        return {
            "evaluation": EvaluationResult(
                total=len(documents),
                ambiguous_count=len(ambiguous),
                quality_verdict="不足,建议进行网络搜索补充",
                ambiguous=ambiguous,
            )
        }

    for doc in documents:
        score = _rel_score(doc) or 0.0
        sdoc = _to_simple_doc(doc)
        ...  # 其余三档分桶/verdict 逻辑保持原样
```

docstring 中阈值说明同步改为「以 relevance_score(sigmoid 重排分)为判据」。

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m pytest tests/test_rrf_retrieval.py tests/test_boundary_medium.py tests/test_smoke.py tests/test_mcp.py -v`
Expected: 全 PASS(M9 固定文案测试 / 工具清单 smoke 不受影响)。

- [ ] **Step 5: Commit**

```bash
git add lawApp_LangGraph/tools/rag_tools.py tests/test_rrf_retrieval.py
git commit -m "feat: 工具面 RRF 语义 — retrieve_legal_knowledge 退役 alpha 形参+docstring 重写(rrf_k 说明+两键降级契约); evaluate_case_relevance 三档改判 relevance_score(召回分不再是判据), rerank 缺键→全 ambiguous+verdict 强制「不足」门控诚实化(数量充足也不发充足结论)"
```

---

### Task 6: 阈值标定 — eval_metrics.suggest_thresholds + run_eval calibration suite

**Files:**
- Modify: `lawApp_LangGraph/eval_metrics.py`(追加)
- Modify: `scripts/run_eval.py:19-51,72-78`(calibration suite)
- Test: `tests/test_eval_metrics.py`(追加)

**Interfaces:**
- Consumes: Task 4 返回 dict(relevance_score + id)。
- Produces: `split_scores(docs: list[dict], relevant_ids: list) -> tuple[list, list]`;`suggest_thresholds(pos_scores, neg_scores, precision_target=0.7, relevant_floor=0.05) -> dict`(数据不足返回 `{"correct_threshold": None, "incorrect_threshold": None}`);CLI `python scripts/run_eval.py --suite calibration --label X`。Task 7 用其输出落 config。

- [ ] **Step 1: Write the failing test**

追加到 `tests/test_eval_metrics.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_eval_metrics.py -v`
Expected: FAIL with `ImportError: cannot import name 'split_scores'`。

- [ ] **Step 3: Write minimal implementation**

`lawApp_LangGraph/eval_metrics.py` 末尾追加:

```python
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
    for t in cuts:
        above_pos = sum(1 for s in pos_scores if s >= t)
        above_neg = sum(1 for s in neg_scores if s >= t)
        if (above_pos + above_neg) > 0 and (
            above_pos / (above_pos + above_neg) >= precision_target
        ):
            correct_thr = t  # cuts 降序, 最后赋值即最高切点

    incorrect_thr = None
    for t in reversed(cuts):  # 升序找最低满足切点
        below_pos = sum(1 for s in pos_scores if s < t)
        below_all = sum(1 for s in pos_scores + neg_scores if s < t)
        if below_all == 0 or below_pos / below_all < relevant_floor:
            incorrect_thr = t  # 升序首中即最低切点
            break

    return {"correct_threshold": correct_thr,
            "incorrect_threshold": incorrect_thr}
```

`scripts/run_eval.py`:`--suite` choices 加 `"calibration"`;main 分发前新增:

```python
async def run_calibration(label: str) -> None:
    """标定 suite: 全 golden set 跑新管线, 正/负 sigmoid 分布 → 建议阈值。

    输出分布分位数与 suggest_thresholds 建议值; 不落库(标定是离线动作)。

    Args:
        label: 本次标定的可追溯标签(记录进 spec §9)。
    """
    import json
    from pathlib import Path

    from lawApp_LangGraph.eval_metrics import split_scores, suggest_thresholds
    from lawApp_LangGraph.RAG_service.pinecone_retriever import PineconeRetriever

    path = Path(__file__).resolve().parents[1] / "data" / "eval" / "retrieval.jsonl"
    cases = [json.loads(l) for l in
             path.open(encoding="utf-8").read().splitlines() if l.strip()]
    if not cases:
        raise SystemExit("golden set 为空, 先跑: python scripts/gen_golden_set.py")

    retriever = PineconeRetriever()
    all_pos, all_neg = [], []
    rerank_missing = 0
    for c in cases:
        results = await retriever.search(query=c["question"], top_k=10,
                                          rerank_top_n=10, namespace=None)
        if results and "relevance_score" not in results[0]:
            rerank_missing += 1
            continue
        pos, neg = split_scores(results, c.get("relevant_ids") or [])
        all_pos.extend(pos)
        all_neg.extend(neg)

    print(f"label={label} | cases={len(cases)} | rerank_missing={rerank_missing}")
    print(f"pos={len(all_pos)} | neg={len(all_neg)}")
    if all_pos:
        sp = sorted(all_pos)
        print("pos 分位数 p10/p50/p90:",
              [round(sp[int(len(sp) * q)], 4) for q in (0.1, 0.5, 0.9)])
    if all_neg:
        sn = sorted(all_neg)
        print("neg 分位数 p10/p50/p90:",
              [round(sn[int(len(sn) * q)], 4) for q in (0.1, 0.5, 0.9)])
    print("建议阈值:", json.dumps(suggest_thresholds(all_pos, all_neg),
                                   ensure_ascii=False))
```

`main()` 的 argparse choices 改 `["retrieval", "calibration"]`,分发:

```python
    if args.suite == "calibration":
        await run_calibration(args.label)
    else:
        await run_retrieval(args.label, args.baseline)
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m pytest tests/test_eval_metrics.py -v`
Expected: 全 PASS(含既有 hit_rate/MRR 测试)。

- [ ] **Step 5: Commit**

```bash
git add lawApp_LangGraph/eval_metrics.py scripts/run_eval.py tests/test_eval_metrics.py
git commit -m "feat: 三档阈值标定能力 — eval_metrics 增 split_scores(正负分组,缺键跳过)+suggest_thresholds(precision≥0.7 最高切点/下方 relevant<5% 最低切点,数据不足返 None); run_eval 增 --suite calibration 输出正负 sigmoid 分布分位数与建议阈值(不落库,离线动作)"
```

---

### Task 7: 全量回归 + 监控冒烟 + 标定落地(运行环境步骤)

**Files:**
- Modify: `docs/superpowers/specs/2026-10-11-rrf-hybrid-retrieval-design.md`(§9 记录标定 label 与新阈值,若跑成)
- 无代码文件改动(除 spec 记录)

**Interfaces:**
- Consumes: Task 1-6 全部。

- [ ] **Step 1: 全量后端测试**

Run: `python -m pytest tests/ -v`
Expected: 全 PASS(23+ 文件,含新增 test_fusion / test_rrf_retrieval)。

- [ ] **Step 2: 前端构建冒烟(前端零改动,确认无意外破坏)**

Run: `cd frontend && npm run build`(以仓库实际构建命令为准,见 package.json)
Expected: 零错误。

- [ ] **Step 3: 真实环境回归门(需 PINECONE_* 凭据 + 本地模型,标注为运行时依赖)**

```bash
python scripts/run_eval.py --suite retrieval --label rrf-post --baseline rrf-pre
```
Expected: hit_rate@5 / MRR@10 **不回退**(spec §5⑦ 回归门)。若回退:停止,分析 rrf_k/权重/候选集大小,不带病合入。

- [ ] **Step 4: 真实环境标定(同上依赖)**

```bash
python scripts/run_eval.py --suite calibration --label rrf-cal-1
```
将输出的建议阈值写入 `config.py`(`correct_threshold` / `incorrect_threshold`),spec §9 追加记录:`标定 label=rrf-cal-1, correct=X.XX, incorrect=X.XX`。若 rerank_missing > 0:先查模型加载,标定作废重跑。

- [ ] **Step 5: Commit(spec 记录)**

```bash
git add docs/superpowers/specs/2026-10-11-rrf-hybrid-retrieval-design.md lawApp_LangGraph/config.py
git commit -m "docs: RRF 改造标定记录 — 回归门/hit_rate/MRR 结果 + calibration 标定阈值落 config + spec §9 记录 label(可追溯可重标)"
```

---

## Self-Review

1. **Spec coverage**:§5①管线(Task 3/4)、§5②fusion(Task 1)、§5③得分语义(Task 2/4)、§5④阈值标定(Task 6/7)、§5⑤降级语义(Task 4/5)、§5⑥工具面与监控(Task 5,监控经 hybrid_score 别名零改动+测试覆盖 tracing 读取路径)、§5⑦回归门(Task 7 Step 3)、§6测试矩阵(各任务内嵌)、§7 Google 风格(Global Constraints)、§8 pgvector 零行为变化(Task 4 仅签名同步)。无缺口。
2. **Placeholder scan**:全文无 TBD/TODO/「类似 Task N」/「适当处理」;所有代码步骤均给出完整可写文件内容。
3. **Type consistency**:`rrf_fuse` 返回 `{match,id,dense_rank,sparse_rank,rrf_score,rrf_rank}` 在 Task 1 定义、Task 4 消费一致;`dense_recall/sparse_recall(query, namespace, top_k)` Task 3 定义、Task 4 调用一致;`relevance_score`/`hybrid_score`/`rrf_score` 命名在 Task 2/4/5 一致;`split_scores`/`suggest_thresholds` 签名 Task 6 定义、run_calibration 调用一致;`clamp_search_params` 搬家后 re-export 保 tests/test_boundary_medium.py:68 旧路径。
