# 边界审查修复(H1-H12 / M1-M15 / L1-L22)实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复全项目审查发现的前后端边界缺陷:12 高危(功能失效/数据错乱/挂死)、15 中危、22 低危(其中 L17/L18 核对为非问题,不改)。

**Architecture:** 后端核心改 `LangGraph_lawApp.py`(TOOL_BY_NAME 运行期化、replanner 空计划收尾)、`api.py`(session 并发锁、resume 守卫、ask_pdf 路由、POST 流端点、POST body 校验)、`utils.py`(session id uuid 化、确认词 LLM 意图判定)、RAG/MCP 三模块(单例修复、NULL 过滤、MCP 重试超时)。前端 `sse.js`(AbortController/done 校验/坏帧容错)、`App.vue`(interrupt 面板失败恢复)、`DocComposer/InterruptPanel`(IME)。测试沿用 `tests/` pytest + 新增边界单测;回归证据落 `tests_ipynb/08_boundary_audit_regression.ipynb`。

**Tech Stack:** 同上轮(Python 3.10+ / LangGraph 1.0.1 / psycopg_async / pytest + pytest-timeout / Vue 3)。

**Spec:** 无独立 spec — 本计划即设计定稿(用户已逐项批准:session 安全走短期方案、确认词走 LLM 意图+短词 fast-path、assistant 流加 POST 端点、49 条全修按优先级)。

## Global Constraints

- 解释器一律 `F:/Anaconda_env/lawApp_langGraph/python.exe`(下称 `$PY`),工作目录一律仓库根
- **所有 pytest 一律**: `$PY -m pytest <目标> -q --timeout=120 --timeout-method=thread -rs`;超时用例记 skipped 不算失败,不阻塞
- 回归证据: 新建 `tests_ipynb/08_boundary_audit_regression.ipynb`,以 subprocess 跑上述 pytest 全量命令并内嵌结果,末尾输出 `ALL PASSED` / `HAS FAILURES` 结论行(格式对齐 tests_ipynb/README.md)
- `.env`(gitignored)只本地编辑不进提交;`.env.example` 有新键才同步
- 六个 interrupt 类型字符串一字不改;`doc_type ∈ {"complaint","defense"}` 不改
- 存量测试文件(tests/ 现有 8 个)只允许**新增**用例到独立新文件,不改老用例;新测试文件命名 `tests/test_boundary_<域>.py`
- 业务错误显式抛出不兜底(仓库既有约定);观测旁路(tracing/audit)记日志放行
- 提交信息: 中文,`C:` 前缀,写明改动文件;一个 Task 一提交
- 行号为审查时快照,可能漂移;**以符号名/结构定位为准**
- 前端改动后 `$PY -m pytest` 不覆盖,验证以 `npx vite build`(frontend/) 通过 + 单元级逻辑人工核对为准;不引入新依赖

## 修复清单总表

| 批 | ID | 问题 | 方案 |
|---|---|---|---|
| 1 | H1 | `TOOL_BY_NAME` 导入期快照,MCP 工具规划了执行不了;checkpoint 旧工具名 KeyError | 改函数 `_tool_by_name(name)` 运行期查 `ALL_TOOLS()`;`build_graph()` 重建供 prompt;executor 用 `.get()`,缺工具→步骤 `status:"error"` |
| 1 | H2 | `POST /ask/pdf` 无装饰器 404 | 补 `@app.post("/ask/pdf")` + try/except 对齐其他端点 |
| 1 | H3 | "结束/收尾"子串误判吞用户补充 | keyword 降级 fast-path(≤4 字纯指令词),其余走 `semantic_confirm`;fast-path 词集:继续/收尾/结束/重试/终止/finish/continue(仅完全相等) |
| 1 | H4 | 同 session 并发流互踩 + reasoning 串流 | `_SESSION_LOCKS: dict[str, asyncio.Lock]`,SSE 入口 `lock.acquire_nowait()` 失败→409 `session_busy`;`_REASONING_BUS` 值改 `set[asyncio.Queue]` fan-out,断开只摘自己 |
| 1 | H5 | resume 无 interrupt 守卫;空 sid 新建线程 | resume 端点先 `aget_state`+`extract_interrupt`,无 pending→400;resume 的空/未知 sid→400 不新建 |
| 1 | H6 | Pinecone 半初始化单例永久降级 | 局部变量构造+附着 index 全成功后才赋 `_service` |
| 1 | H7 | pgvector NULL embedding 崩检索 | SQL 加 `WHERE embedding IS NOT NULL`;循环 `r[5] is None: continue` |
| 1 | H8 | MCP 连接失败永久缓存 + 启动挂死 | `_loaded=True` 移成功后,失败可重试;`get_tools()` 包 `asyncio.wait_for(10s)`;`__aenter__()` 正确持有至 close_mcp |
| 1 | H9 | PDF 文件名路径穿越 | `basename` + `re.sub(r'[^\w\-.\u4e00-\u9fff]','_')`,拒空/点开头;`len()` 日志移 try 内 |
| 2 | H10 | SSE 无取消/断流当完整答案 | store 持 AbortController;`readSSE` 追 `sawDone`,流尽无 done/error→`state.error="连接中断"`;`JSON.parse` 单帧 try/catch;ModeSwitch 切换先 abort |
| 2 | H11 | resume 失败永久丢 interrupt 面板 | 不先清,首个成功流事件后清;catch 恢复局部副本;`GET /sessions/{sid}` 重建面板 |
| 2 | H12 | `keyup.enter` IME 误提交 | 两处 `@keydown.enter` + `if (e.isComposing || e.keyCode===229) return` |
| 3 | M1 | pgvector rerank/top_k 无钳制 | `max(1, min(rerank_top_n,10))`;`top_k` clamp 1..50 两后端统一 |
| 3 | M2 | planner 每次 new AsyncOpenAI 泄漏+无超时 | 模块级单例 `timeout=60` |
| 3 | M3 | replanner 空 plan 无限循环 | 空计划→"材料不足"直接 finalize;`replan_check` 加空 plan 检测 |
| 3 | M4 | `semantic_confirm` 抛错在 try 外 | 移进端点 try;LLM 失败降级短词匹配 |
| 3 | M5 | 断开 trace 记 "ok" | `_run_sse`/tracing 捕 `CancelledError/GeneratorExit`→`status="cancelled"`;`_astream` try/finally 落 span |
| 3 | M6 | session_id 撞号/乱注 | id 改 `uuid4` 格式 `XX-<uuid>`;`ensure_session` 校验格式非法→400 |
| 3 | M7 | MCP store 静默分叉 | PG store 失败→记忆工具返回 `status:"error"`,不挂 InMemory;`_get_server_store` 加锁+关池 |
| 3 | M8 | query 补充无上限累积 | state 新增 `user_supplements: list[str]`;HITL 答案进列表;prompt 组装处拼接;原 query 不变(并入 L16 截断统一) |
| 3 | M9 | 异常原文进对话 | `str(e)[:200]`→固定中文文案+logger.warning(对齐 db_tools) |
| 3 | M10 | 空 LLM 流返回成功空答案 | answer 空→`status:"error"` |
| 3 | M11 | GET 4000 CJK 超 URL 限 | 后端加 `POST /assistant/ask/stream`(body 传参,SSE 响应不变),GET 保留;textarea `maxlength="4000"`;前端 sse.js 支持 POST body |
| 3 | M12 | HistorySidebar 乱序覆盖+裸 await | `open()` try/catch + `openSeq` 守卫;失败写 error ref;`setSession` 移 fetch 成功后 |
| 3 | M13 | 双模式共用 localStorage sid | key 拆 `lawapp_sid_att`/`lawapp_sid_asst`;`ensure_session` 校验前缀匹配模式 |
| 3 | M14 | db pool 竞态/坏池缓存 | `get_pool` 加 `asyncio.Lock`;`ensure_tables` 失败→`_pool=None` 再抛;close 先置 None;`timeout=5` |
| 3 | M15 | `aget_state` 在 try 外 | `_finalize_or_interrupt`/`GET /sessions` 包 try,失败返回已有结果+warning |
| 4 | L1 | `_safe_audit` 静默吞 | 加 `logger.warning(..., exc_info=True)`;`record_feedback` 统一吞+日志 |
| 4 | L2 | sse_event 无 default=str | 加 |
| 4 | L3 | embed 维度硬编码 1024 | 两处改 `dims: settings.embed_dim` |
| 4 | L4 | search_memory fallback 裸跑 | 再包 try→`status:"error"` |
| 4 | L5 | evaluate_case_relevance 收 dict 产假结论 | dict→尝试取 `rag_documents`,仍错→`status:"error"`;`PromptsRecord(**)` 包 try |
| 4 | L6 | get_Documents 空文档 IndexError | 空校验返回错误;`metadata.get("chunk_text","")` |
| 4 | L7 | MCP search_cases 静默 cap | 透传 top_k,clamp 1..50 |
| 4 | L8 | health_check 触发全冷启动 | 轻量返回 `{"status":"ok"}`,不触发模型加载 |
| 4 | L9 | CitationList 空值链 | `?.` 链 + `filter(Boolean)` + 默认标题"未命名" |
| 4 | L10 | Typewriter interval 无卸载清理 | `onBeforeUnmount` 清理 |
| 4 | L11 | 会话列表只拉一次 | 新 sid 事件/切会话后 `loadSessions()` |
| 4 | L12 | 截断阈值 120 两处魔数 | 抽 `store.js` 导出常量 `COLLAPSE_LEN` |
| 4 | L13 | api.js 死函数 | 删除三个未用函数 |
| 4 | L14 | 空气泡先入对话 | `checkOk` 失败时回滚末条 user+assistant 消息 |
| 4 | L15 | degrade one-shot | `degrade_used` 改计数,每 `threshold*2` 次失败可再询问 |
| 4 | L19 | 缺 DEEPSEEK_API_KEY 迟爆 | config 启动校验 fail loud(策略对齐 Pinecone key) |
| 4 | L20 | create_index 就绪循环无超时 | 总超时 10 分钟;`failed` 态直接抛 |
| 4 | L22 | tracing list 形式 content 不聚积 | 非纯文本走 `str(c)` 拼接分支 |

(L17/L18 核对为非问题,无动作。)

---

### Task 1: 后端高危 H1-H9

**Files:**
- Modify: `lawApp_LangGraph/LangGraph_lawApp.py`(TOOL_BY_NAME 函数化、executor .get、H3 节点侧 fast-path)
- Modify: `lawApp_LangGraph/FastAPI/api.py`(H2 ask_pdf、H4 锁+bus、H5 守卫)
- Modify: `lawApp_LangGraph/FastAPI/utils.py`(H3 normalize_resume、H5 空 sid)
- Modify: `lawApp_LangGraph/RAG_service/pinecone_retriever.py`(H6)
- Modify: `lawApp_LangGraph/RAG_service/pgvector_retriever.py`(H7)
- Modify: `lawApp_LangGraph/mcp/mcp_client.py`(H8)
- Modify: `lawApp_LangGraph/tools/tools.py`(H9)
- Test: `tests/test_boundary_high_backend.py`(新建)

**Interfaces:**
- Produces: `_tool_by_name(name: str) -> Optional[Any]`;`_SESSION_LOCKS`/`try_acquire(sid)`;`_REASONING_BUS: dict[str, set[Queue]]`;resume 端点 400 语义 `no_pending_interrupt`/`invalid_session_id`;SSE 409 `session_busy`。
- H4 注意: 锁释放与 `close_reasoning_channel` 在 finally;`open_reasoning_channel` 返回每流独立 queue。向后兼容: bus 只剩单订阅时行为与旧一致。

- [x] Step 1: 写失败测试(TDD): H1 注册后置 fake MCP 工具→planner 计划含它→可执行;H5 双重 resume→第二次 400;H7 插 NULL 行检索不炸;H9 `../../evil.pdf` 落 output_dir 内;H3 "婚姻关系已于2020年结束" 不判 finish(semantic_confirm 用 stub)
- [x] Step 2: 实现 H1-H9 全部改动
- [x] Step 3: `$PY -m pytest tests/test_boundary_high_backend.py -q --timeout=120 --timeout-method=thread` 全绿
- [x] Step 4: 全量回归 `$PY -m pytest tests/ -q --timeout=120 --timeout-method=thread -rs`(超时跳过不算失败)
- [x] Step 5: 提交 `C: 边界修复高危后端批 H1-H9 — ...`(列改动文件)

### Task 2: 前端高危 H10-H12

**Files:**
- Modify: `frontend/src/sse.js`(POST body 支持、sawDone、坏帧容错)
- Modify: `frontend/src/store.js`(AbortController、COLLAPSE_LEN 常量)
- Modify: `frontend/src/App.vue`(abort 接线、interrupt 恢复、ModeSwitch busy guard、GET→POST)
- Modify: `frontend/src/components/{DocComposer,InterruptPanel,ModeSwitch}.vue`
- 验证: `cd frontend && npx vite build` 通过 + 逻辑核对清单

- [x] Step 1: sse.js 重构: `streamConsult(url, onEvent, {signal, method, body})`、`readSSE` 返 `{sawDone}`、坏帧跳过
- [x] Step 2: App.vue: AbortController 生命周期、submit/resume 传 signal、首个流事件后才清 interrupt、catch 恢复副本、`restoreInterruptFromServer(sid)`、校验失败回滚空消息(L14 顺带)
- [x] Step 3: ModeSwitch 切换 abort+reset;DocComposer/InterruptPanel IME keydown
- [x] Step 4: `npx vite build` 通过;核对项逐条打勾(H10 断流错误横幅/取消/坏帧,H11 面板恢复,H12 上屏不发送)
- [x] Step 5: 提交 `C: 边界修复高危前端批 H10-H12 — ...`

### Task 3: 中危 M1-M15

**Files:** api.py、utils.py、LangGraph_lawApp.py、state.py、tracing.py、db.py、RAG_service/pgvector_retriever.py、mcp/mcp_server.py、tools/{tools,rag_tools}.py、前端 {HistorySidebar,store}.js;Test: `tests/test_boundary_medium.py`(新建)

- [x] Step 1: 写失败测试: M1 rerank 0→不空;M3 replanner 空 plan→finalize 不触 recursion_limit;M6 非法 sid→400;M10 空 answer→error;M15 aget_state 失败→仍返回答案
- [x] Step 2: 实现 M1-M15(方案见总表;M8 新增 state 字段需 bump state.py 并保证旧 checkpoint 兼容——缺字段用默认工厂)
- [x] Step 3: 目标测试全绿 + 全量回归(命令同上)
- [x] Step 4: `npx vite build` 通过
- [x] Step 5: 提交 `C: 边界修复中危批 M1-M15 — ...`

### Task 4: 低危 L1-L22(除 L17/L18)

**Files:** db.py、api.py、runtime.py、mcp_server.py、RAG_program.py、tools/{db_tools,rag_tools}.py、config.py、前端 CitationList/TypewriterText/HistorySidebar/api.js/store.js;Test: `tests/test_boundary_low.py`(新建)

- [x] Step 1: 写失败测试: L1 audit 失败有日志(caplog);L4 fallback 失败→status error;L5 dict 输入→status error;L19 缺 key 启动抛
- [x] Step 2: 实现 L1-L22 总表所列(前端项核对为主)
- [x] Step 3: 目标测试全绿 + 全量回归
- [x] Step 4: `npx vite build` 通过
- [x] Step 5: 提交 `C: 边界修复低危批 L1-L22 — ...`

### Task 5: 回归证据 ipynb + 收官

**Files:**
- Create: `tests_ipynb/08_boundary_audit_regression.ipynb`
- Modify: `docs/superpowers/plans/2026-09-21-boundary-audit-fixes.md`(勾选框)

- [x] Step 1: 新建 notebook: cell 1 说明,cell 2 subprocess 跑 `$PY -m pytest tests/ -q --timeout=120 --timeout-method=thread -rs`、cell 3 解析末行输出结论 `ALL PASSED`/`HAS FAILURES`(超时 skipped 不算失败)
- [x] Step 2: `$PY scripts/run_nb.py tests_ipynb/08_boundary_audit_regression.ipynb` 实跑,确认全绿
- [x] Step 3: 计划文档勾选收官;提交 `C: 边界修复收官 — 回归 ipynb 全绿 ...`
- [x] Step 4: push 分支 `fix/boundary-audit-p0p1`

---

## 执行记录(2026-09-21 收官)

| 批 | 提交 | 内容 | 测试 |
|---|---|---|---|
| 高危前端 | 85d367f | H10-H12 + L14 | vite build 过 + 逻辑核对清单 |
| 跑测脚本 | afb4f3d | 纯超时失败记 SKIPPED | — |
| 高危后端 | c315689 | H1-H9 + H10 后端(SSE ping) | test_boundary_high_backend 24 绿; 全量 4 PASS/5 SKIP/0 FAIL |
| 中危 | 7c2bb5c | M1-M15 | test_boundary_medium 27 绿; 组合 65 绿; 全量 5 PASS/4 SKIP/1 FAIL(抖动, 复跑绿) |
| 低危 | f9301df | L1-L22(除 L17/L18 非问题) | test_boundary_low 17 绿; 全量 7 PASS/4 SKIP/0 FAIL |
| 收官 | 本提交 | 回归 ipynb + 计划勾选 | 见下 |

**最终回归**: tests_ipynb/08_boundary_audit_regression.ipynb(run_all 实跑, --save 回写输出)
- 全量 10 个测试文件: **7 PASS / 4 SKIPPED / 0 FAIL, ALL PASSED**
- SKIPPED 4 文件(test_sessions_degrade/test_smoke/test_trace_db/test_trace_e2e)为 PG 容器不可用(Docker 引擎 WSL 挂载 vhdx E_ACCESSDENIED, 机器级问题)导致的环境超时, 按约定跳过; 修复批中 M14/H8 已使这些路径具备快速失败语义
- 注: test_tracing::test_node_registration_wrapped_with_traced 存在偶发抖动(全量回归中出现 1 次, 累计复跑 7+ 连绿), 已观察未复现根因

**偏差决策存档**:
1. H1 步骤失败状态用 "failed" 而非 "error"(StepStatus Literal 不变量)
2. H8 依 langchain-mcp-adapters 0.3.2 实际 API: MultiServerMCPClient 非 async context manager, 去掉 __aenter__/__aexit__ 调用
3. H3 存量断言随 M8 规格冲突做意图保留修改(query→user_supplements)
4. test_boundary_medium 加 _ENV_KEYS 快照防 api.py load_dotenv 同进程污染存量断言

**补录(PG 恢复后复跑, 2026-09-21)**: postgres-vector 容器重启恢复后全量复跑
- 4 个此前 SKIPPED 文件全部真实通过(test_sessions_degrade/test_trace_db/test_trace_e2e 直接过; test_smoke 1 断言随 M8 改为断言 user_supplements 后 11/11 过 — 与 H3 预算测试同型偏差, 偏差决策 3 已涵盖)
- 回归 ipynb 复跑(--save 回写): **11 批 / 0 SKIPPED / 0 FAIL, ALL PASSED**
- test_tracing 偶发抖动本轮未复现
