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

### 增补(用户批准, 与初版两处变更)

- **A1 摘要字数截断全去除**: prompt 组装层的逐条字符截断全部移除
  — law content[:80]×3 处(_step_summaries / _extract_doc_fields /
  finalize laws_digest)、web snippet[:80]、cases chunk_text[:100]、
  通用兜底 chunk_text[:300]。条数上限(×5/×3)保留。LLM 从此看到
  检索原文全文。
- **A2 llm 回答内容折叠**: 聊天页 result_llm 气泡(完成态 assistant
  回答, ChatView)加与用户气泡同款的模糊折叠效果(max-h-24 限高 +
  mask-image 渐变 + 展开/收起), 阈值同 COLLAPSE_LEN, 超长回答不撑屏。

## 3. 非目标

- 不回填存量 span(旧数据已字符串化, 不可逆; 修复只对新 run 生效)。
- 不改监控 API 契约(MonitorSpan 透传, JSONB 列 psycopg 自动反序列化)。
- 不做"检索↔LLM 调用"引用链接(后续增强)。

## 4. 设计(四段)

### ① 源头全文 + 摘要截断去除

- `db_tools.py` `fetch_laws` 删 `content[:600]`。law_results 全文进入
  state 与落库。
- `LangGraph_lawApp.py` 摘要构建的逐条字符截断全删: `_step_summaries`
  web snippet[:80]/law content[:80]、`_extract_doc_fields` laws_digest
  [:80]、finalize laws_digest[:80] + cases_digest[:100]、通用兜底
  chunk_text[:300]。条数上限(×5/×3)保留。LLM prompt token 量随之上涨
  (法条全文/检索原文进上下文), 用户已批准。

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

> 实施修正: 排查发现 `_emit_llm_span` 已有 `_msg_text` 把 BaseMessage
> 映射为 `{role, content}` dict, planner `record_llm_span` 走裸流路径
> 传 `[prompt_text]` 纯字符串(JSON 原生) — 字符串化真凶只是 ② 的
> `default=str`。故无需新增 helper, ③ 收窄为 `_msg_text` 的 role 映射
> 改进: `human→user` / `ai→assistant`(API 侧命名), langchain 内部
> 字段(id/additional_kwargs 等)继续不落, 非 message 对象原样透传。

`tracing.py` `_msg_text` role 映射改进(见上), 落库的 llm span input
即纯 `[{role, content}]` 数组(planner 裸流保持纯字符串 prompt)。

### ④ 前端抽屉就地展开

- tool span: 展开 → output 的 JsonTree(原始检索全文, 分层)。
- llm span: 展开 → input 的 messages 树 + output 回答文本(JsonTree
  自带长文截断 + title 悬浮, 不另做折叠)。
- A2 折叠落在聊天页 result_llm 气泡(ChatView, 见 §2 增补), 不在抽屉。

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
- `_msg_text` 单测: 各 message 类型 role 映射(human→user/ai→assistant)、
  非 message 对象透传。
- 摘要去截断单测: `_step_summaries`/laws_digest 组装函数对长 content
  不再截断(全文进摘要)。
- 前端 `npm run build` 零错误。
- 冒烟: 新跑一轮律师助理对话 — 聊天页超长回答折叠渐变可展开; 抽屉
  验证法条全文可见、llm messages 树分层、无字符串包裹、无 Vue 告警。

## 7. 风险与边界

- 落库体积: tool span output 与 node state 快照随全文增大(条文千字级,
  top_k 有限)。
- token 成本(A1 生效后): executor/planner/finalize/抽取 各环节 prompt
  内嵌检索原文全文, 单轮调用量涨; 用户已知情批准。
- `exclude_none=True` 与默认空串语义: LawsResult 等模型字段均带默认,
  不依赖 None 区分, 安全。
- 存量 run: 465 spans 保持字符串形态, 不迁移。
