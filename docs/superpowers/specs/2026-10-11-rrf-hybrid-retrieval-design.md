# Pinecone 检索 RRF 改造: 双路召回 + 倒数排名融合 + 重排出分 — 设计

日期: 2026-10-11
状态: 已确认(brainstorming 三问决策全部收敛, 用户批准)
前置: 现状分析全链路(rag_tools / RAG_service / eval_metrics / run_eval 均已核对)

## 1. 背景与问题

现状 Pinecone 混合检索为凸组合方案: `hybrid_convex_scale` 将稠密向量
(BGE 1024 维归一化)乘 α、稀疏向量(BM25)乘 (1−α) 后单次提交
Pinecone dotproduct 查询, 服务端返回 `score = α·cos + (1−α)·BM25 重合`
即 `hybrid_score`。

问题:
- α 调参困难: BM25 项无上界、余弦有界, 两路量纲不可比, 凸组合的
  权重没有可解释的标定方法。
- 稠密/稀疏两路得分从未独立存在, 无法观察单路行为, 排查困难。
- `hybrid_score` 实质是召回分; CrossEncoder 重排只改排序不回写分数,
  CRAG 三档阈值(0.5/0.2)判的是重排前的召回分, 排序与档位脱节。

## 2. 目标

1. 凸组合替换为 **RRF(Reciprocal Rank Fusion)**: 稠密/稀疏双路独立
   召回, 客户端按 `rrf = Σ 1/(k + rank)` 融合, 只用排名不用原始分,
   天然免疫量纲差异。
2. CrossEncoder 重排后**产出最终得分** `relevance_score =
   sigmoid(重排 logits)`, 作为 CRAG 三档门控的唯一绝对信号源。
3. 三档阈值改为 golden set 实测标定, 不拍脑袋。
4. 降级路径诚实: rerank 不可用时门控不发「充足」结论。

## 3. 非目标

- **pgvector 后端零改动**(案例库 + 法条库均不引入混合检索, 详见 §8)。
- 不回填存量数据; 不改监控 API 契约(JsonTree 泛化渲染, 无前端改动)。
- 不做检索↔LLM 调用引用链接等监控增强。
- 不动 `rerank_top_n` 之外的工具参数面(top_k 语义不变)。

## 4. 决策记录(brainstorming 收敛结果)

| 决策点 | 结论 | 理由 |
|---|---|---|
| 改造范围 | 只改 Pinecone | 已有双路 + golden set 现成验证; pgvector 无稀疏路且 ingest 元数据欠账 |
| 分档判据 | sigmoid(CrossEncoder 分) | RRF 是排名相对分: 全垃圾召回时照样满分, 门控被骗; CrossEncoder 绝对分才能识别「全垃圾」 |
| 三档阈值 | golden set 实测标定 | 运营点: correct 线取 precision≥0.7 切点, incorrect 线取下方 relevant<5% 切点 |
| rerank 必要性 | 保留并升格 | RRF 只解决融合粒度; 候选集内部精细区分仍靠 CrossEncoder; 且它是门控唯一绝对信号源 |
| rerank 失败/禁用 | 全 ambiguous + verdict 不足 | 没有绝对分不发「充足」; replanner 走网络补充 |
| 单路查询失败 | 单路降级(优先 dense)+ 日志 | RRF 退化为单路排名; 两路全挂维持 status=error 现状 |
| 归一化 | sigmoid, 否决批内 min-max | min-max 是查询相对分, 同文档跨查询分数漂移, 门控不可比 |

## 5. 设计

### ① 管线总览

```
query ──┬─ BGE embed → Pinecone 查①(纯向量, top_k) ─┐
        └─ BM25 encode → Pinecone 查②(纯稀疏, top_k) ┘
                                    ↓ 客户端 RRF 融合(fusion.py, k=60)
                        候选集 top max(2×rerank_top_n, 20)
                                    ↓ CrossEncoder(bge-reranker-large)
                    最终排序 + relevance_score = sigmoid(logits)
                                    ↓
              state: rrf_score(溯源) + relevance_score(决策)
```

两路查询并发: Pinecone SDK 同步, 各自 `asyncio.to_thread` 后
`asyncio.gather`。`hybrid_convex_scale` 凸组合路径**全仓删除**, 不留
死代码; `alpha` 参数从 `search` 接口与工具签名退役。

### ② 新模块 `RAG_service/fusion.py`(纯函数)

```python
def rrf_fuse(dense_matches, sparse_matches, k=60, w_dense=1.0,
             w_sparse=1.0, top_n=20):
    """按 vector id 聚两路排名做 RRF 融合。

    Args:
        dense_matches: 稠密路召回(Pinecone Match 列表, 已按本路得分排序)。
        sparse_matches: 稀疏路召回, 同上。
        k: RRF 平滑常数, 默认 60(业界惯例)。
        w_dense / w_sparse: 两路权重, 默认等权。
        top_n: 融合后截断的候选集大小。

    Returns:
        按 rrf_score 降序的候选列表, 每条含
        dense_rank / sparse_rank / rrf_score(值域 ≤ 2/(k+1))。
        仅单路出现的文档另一路贡献 0。
    """
```

rrf_k / rrf_w_dense / rrf_w_sparse 进 config.py; 单测全覆盖。

### ③ 得分语义与 state 变更

`RetrievedDocument`(state.py)增加字段:

```python
rrf_score: float = 0.0        # 融合分, 仅日志/监控/调试, 不进任何决策路径
relevance_score: float = 0.0   # 最终分 = sigmoid(CrossEncoder logits)
hybrid_score: float = 0.0     # 兼容别名 = relevance_score
                                 # (旧 checkpoint / score.py rag_top_score /
                                 #  监控页读它, 默认值兜底旧数据)
```

`evaluate_case_relevance` 三档改判 `relevance_score`, 阈值取 §④ 标定值。
分档结构/quality_verdict/min_quality_docs 逻辑不动。

### ④ 三档阈值标定(不拍脑袋)

`run_eval.py` 新增 `--suite calibration`:

1. golden set 30 条跑完整新管线(双路召回 → RRF → rerank), 收集
   relevant_ids 内/外文档的 sigmoid 分布。
2. 自动建议阈值: `correct_threshold` = 满足 precision ≥ 0.7 的最高切点;
   `incorrect_threshold` = 下方 relevant 占比 < 5% 的切点。
3. 交叉验证 `min_quality_docs=3`: 中位查询 rerank_top_n=5 里 ≥3 条过
   correct 线; 不满足则输出「降阈值 or rerank_top_n 提 8」的数据依据,
   由人定夺。
4. config.py 阈值改为标定值, 本 spec §9 记录标定 run label, 可追溯可重标。

### ⑤ 降级语义

- **rerank 禁用(RERANK_ENABLED=0)/失败**: 检索文档全归 ambiguous 档,
  `quality_verdict` 必判「不足,建议进行网络搜索补充」→ replanner 走
  网络补充。门控不骗人。
- **单路查询失败**: 降级单路(优先 dense 路)+ warning 日志, RRF 退化为
  单路排名; 两路都失败 → 维持现状(status=error 固定中文文案, 异常原文
  只进日志)。
- rerank 默认永远开启; 失败路径是防御(模型加载网络抖动/OOM/CUDA 失配,
  见 embedder.py 现有 try/except), 不是常态。

### ⑥ 工具面与监控

- `retrieve_legal_knowledge` docstring 重写: 删 alpha 行, 加 rrf_k 说明
  (docstring 是 planner 的工具说明书, 必须同步); `top_score` 日志改取
  rank-1 的 relevance_score。
- `score.py` `rag_top_score` 分量读 hybrid_score 字段, 别名兜住, 不改。
- 监控页 JsonTree 泛化渲染 dict, 前端零改动, 浏览器冒烟验证即可。

### ⑦ 回归门

改造后 `run_eval.py --suite retrieval --baseline <改造前 label>`,
**hit_rate@5 / MRR@10 不得回退** — 换融合算法没伤效果唯一客观证据。

## 6. 测试

| 层 | 内容 |
|---|---|
| fusion 单测 | 双路重叠/单路独占/k 边界/权重/截断/rrf 值域(≤2/(k+1)) |
| retriever 契约(fake index) | 双路融合→重排→字段断言; 单路降级; rerank 失败全 ambiguous |
| PG 往返 | 新字段经 `_trace_json` 落库解包(pydantic model_dump, 对齐 769ab92 模式) |
| calibration | golden set 分布空集/单点容错 |
| 全量 | tests/ 23 文件 + vite build + 浏览器冒烟(监控 span 树看 rrf_score/relevance_score) |

## 7. 代码风格

本次改动所有新增/修改函数(fusion.py 全部、search_rrf、retriever.search
修改处、契约测试)一律 Google 风格多行 docstring — `Args:` / `Returns:`
/ `Raises:` 分节, 中文描述, 对齐仓库现有惯例(fetch_laws / add_document
同款); 行内注释保持「只写代码本身说不出的约束」。

## 8. pgvector 缓做条款(写死, 防蔓延)

pgvector 侧(案例库 law_cases + 法条库 law_vector 均纯 dense)**本次
零改动**, 不引入混合检索。两个观察条款:

- a. 案例库: pgvector 先修 ingest 元数据解析(case_cause 空/case_number
  = 文件名) → 建 pgvector 版 golden set → dense-only hit_rate@5 < 0.7
  才立项稀疏路; 立项时用 rank_bm25 进程内方案(4000 chunks 毫秒级, 零
  DDL), 不用 PG tsvector。
- b. 法条库: 精确实引查询(「民法典第1079条」)若出现 miss, 走结构化
  元数据前置查询补丁(SQL 精确匹配 article_number/law_title 命中, dense
  兜底), 不走向量混合 — 精确法条号是结构化数据, 元数据查询比任何
  向量/词频方案都准且零额外基础设施。

## 9. 实施顺序

```
1. fusion.py + 单测(纯函数, 零风险)
2. search_rrf + PineconeRetriever 切换 + retriever 契约测试
3. run_eval retrieval suite baseline diff(回归门, 不回退才继续)
4. calibration suite → 标定阈值落 config(标定 label 记录于此)
5. 降级语义 + docstring + 监控字段收尾
6. spec 定稿 + 全量回归 + 浏览器冒烟
```

### 9.1 标定与回归记录(2026-10-11, 7 任务全部落地)

- **回归门(rrf-post vs baseline-001)**: hit_rate@5 0.6667→0.7333(+0.0667),
  MRR@5 0.5500→0.6667(+0.1167), precision/recall/f1 全升, 门通过。
- **标定 run rrf-cal-1**: 30 例, pos 93 / neg 207, pos 分位数
  0.5308/0.7225/0.7303, neg 0.5071/0.6594/0.7276, 正负分布重叠大
  (跨案例 chunk 主题相近), 建议阈值已落 config:
  correct_threshold=0.731, incorrect_threshold=0.5005。
- **已知限制 — 索引形态阻塞稀疏路**: 存量索引 pinecone-test-lawapp
  创建时显式 vector_type="dense"(纯稠密), Pinecone 拒绝纯稀疏查询
  ([400] Cannot query index ... with only sparse vector), 全量查询实际
  降级为密集单路 — 上述回归/标定均为降级态结果。create_index 已修
  (去掉 vector_type 参数, 新建索引即混合可查); 存量索引须删建 + 全量
  重传后稀疏路才生效, 届时重跑 --suite calibration 重标阈值。
  降级语义(单路存活优先/缺键标记)按设计工作, 全量回归 250 项零失败。
