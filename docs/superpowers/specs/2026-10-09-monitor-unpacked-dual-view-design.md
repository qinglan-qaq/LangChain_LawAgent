# 监控页双视图: 原始检索全文 + LLM 输入纯 JSON — 设计

日期: 2026-10-09
状态: 已确认(用户批准方案A)
前置: 2026-10-09-attorney-assistant-docx-upgrade-design.md(监控页/抽屉/JsonTree 均为该期产物)

## 1. 背景与问题

监控页运行详情抽屉中, 检索类 tool span 的 output 与 llm span 的 input
目前以"字符串形式的 JSON 结果"呈现, 无法分层浏览。根因不在截断也不在
解包链路本身, 而在观测层两处:

- 落库序列化 `_trace_json`(db.py:305)用 `json.dumps(default=str)`,
  pydantic 对象(`LawsResult` / `simpleRetrievedDocument` / langchain
  `BaseMessage` / state 内要素模型)不被 JSON 序列化认, `default=str`
  把每条整条 repr 成字符串。
- `fetch_laws`(db_tools.py:266)出库即截 `content[:600]`, "原始检索"
  在源头就只剩 600 字(PG 库存全文)。

同时确认: 运行时解包链路(工具→pydantic→merge_node json.loads→
强类型 state)完好; LLM 实际收到的是 80-300 字截断摘要(prompt 组装层),
这是刻意的 token 优化, 本设计不改。

## 2. 目标

监控页运行详情抽屉内, span 就地展开可见两份完全解包的数据:

1. **原始检索内容(未截断)** — tool span output: 法条全文、RAG chunk
   全文、web 结果, 结构化 JSON 树, 无字符串包裹。
2. **真正进入 LLM 的内容** — 全部 llm span 的 input: 每次 LLM 调用
   (规划/执行/抽取/确认/终答/闲聊)的完整 messages, 精简为
   `[{role, content}]` 纯数组, 无 langchain 内部冗余字段
   (id/additional_kwargs/response_metadata/type 前缀等), 无字符串包裹。

## 3. 非目标

- 不改 LLM prompt 组装与截断档位(80-300 字摘要设计保持)。
- 不回填存量 span(旧数据已字符串化, 不可逆; 修复只对新 run 生效)。
- 不改监控 API 契约(MonitorSpan 透传, JSONB 列 psycopg 自动反序列化)。
- 不做"检索↔LLM 调用"引用链接(本期就地展开已够, 链接属后续增强)。

## 4. 设计(四段)

### ① 源头全文

`db_tools.py` `fetch_laws` 删 `content[:600]`。law_results 全文进入
state 与落库。LLM 侧摘要 `[:80]`(LangGraph_lawApp.py `_step_summaries`
:1161 / laws_digest :1204/:2117)不动, token 用量零变化。影响面仅为
state 快照与 tool span 落库体积增大(法条条文千字级, top_k 有限, 可接受)。

### ② 落库解包(单点修复)

`db.py` `_trace_json` 的 default 改为:

```python
def _json_default(o):
    if isinstance(o, BaseModel):
        return o.model_dump(exclude_none=True)
    return str(o)
```

一处改动, tool span output / llm span input / node span state 三类
span 的 pydantic 成员全部自动解包为 dict。非 pydantic 未知对象仍 str
兜底(观测旁路永不抛)。

### ③ LLM 输入精简

`tracing.py` 新增 `_slim_messages(msgs)`:

- langchain BaseMessage → `{"role": role 映射(human→user 等),
  "content": content}`; content 为 list(多模态块)时原样保留。
- 非 BaseMessage 项原样通过(兼容测试替身 str/dict)。
- `_emit_llm_span` 与 `record_llm_span` 两个出口统一调用, 落库的
  llm span input 即纯 `[{role, content}]` 数组。

### ④ 前端抽屉就地展开

- tool span: 展开 → output 的 JsonTree(原始检索全文, 分层)。
- llm span: 展开 → input 的 messages 树 + output 文本内容。
- 复用现有 JsonTree(折叠/长文截断+title 悬浮), 补语义 id
  (`span-detail-{i}` 等), 遵循全前端唯一 id 惯例。

## 5. 数据形态(修复后示例)

tool span output(fetch_laws):

```json
{"law_results": [{"law_title": "中华人民共和国民法典",
  "chapter": "婚姻家庭编", "article_number": "1079",
  "content": "<全文>"}], "status": "success", "count": 5,
  "top_similarity": 0.83}
```

llm span input:

```json
[{"role": "system", "content": "你是资深婚姻家事律师助理…"},
 {"role": "user", "content": "…完整 prompt 文本…"}]
```

## 6. 测试

- `_json_default` 单测: pydantic→dict、嵌套 list/dict 内 pydantic→dict、
  非法对象→str 兜底、观测旁路不抛。
- `fetch_laws` 断言更新: 600→全文(与 PG 测试数据实际长度对齐)。
- `_slim_messages` 单测: 各 message 类型 role 映射、list content 原样、
  非 BaseMessage 透传。
- 前端 `npm run build` 零错误。
- 冒烟: 新跑一轮律师助理对话, 抽屉验证 — 法条全文可见、llm messages
  树分层、无字符串包裹、无 Vue 告警。

## 7. 风险与边界

- 落库体积: 全文法条 + 完整 messages(法条全文会随摘要文本进 llm
  input — 注: 摘要仍是 80 字, llm input 不因此变大; 变大的只有
  tool span output 与 node state 快照)。
- `exclude_none=True` 与默认空串语义: LawsResult 等模型字段均带默认,
  不依赖 None 区分, 安全。
- 存量 run: 465 spans 保持字符串形态, 不迁移。
