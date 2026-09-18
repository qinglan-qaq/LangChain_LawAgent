# tests_ipynb — 配置可用性体检册

以 Jupyter notebook 形式体检「当前 `.env` 配置到底哪些功能可用」。每册独立可跑，末尾输出
PASS/FAIL/SKIP 汇总表与 `ALL PASSED` / `HAS FAILURES` 结论。

与 `tests/` 的分工：`tests/` 是 pytest 回归（不依赖外部资源，CI 用），本目录是**环境体检**
（会真实连库、调 LLM、起子进程），回答「这台机器上这套配置现在能干什么」。

## 怎么跑

```bash
PY="F:/Anaconda_env/lawApp_langGraph/python.exe"

$PY tests_ipynb/run_all.py            # 全部（约 10 分钟，含 LLM 调用与模型冷启动）
$PY tests_ipynb/run_all.py 01 05      # 只跑文件名含 01 / 05 的册
$PY tests_ipynb/run_all.py --save     # 同时把执行结果写回 .ipynb，便于在 Jupyter 里看输出
$PY scripts/run_nb.py tests_ipynb/03_postgres.ipynb   # 单册
```

退出码：任一册为 `HAS FAILURES` 返回 1（可接 CI）。也可直接用 Jupyter/VSCode 打开，
内核选 **Python (LawApp)**（kernelspec 名 `lawapp`，指向 `F:\Anaconda_env\lawApp_langGraph`）。

## 状态语义

| 状态 | 含义 |
|---|---|
| PASS | 该项实测可用 |
| FAIL | 该项不可用，或前置不满足导致功能失效；detail 里给修复方向 |
| SKIP | 前置条件不满足（如无密钥）或显式禁用，**不算失败**，但结论里会说明原因 |

## 各册覆盖

| 册 | 测什么 | 外部依赖 |
|---|---|---|
| 01 环境配置 | `.env` 位置与内容、pydantic 与 `os.getenv` 两条读取路径的一致性、必填凭据、数据文件、模型缓存、日志目录 | 无 |
| 02 LLM 连通性 | 两个模型 × 三条调用形态（普通对话 / `bind_tools` / `with_structured_output`）能力矩阵，并映射到受影响的图节点 | DEEPSEEK_API_KEY（会真实调用，约 8 次小请求） |
| 03 PostgreSQL | 建连、pgvector 扩展、业务表与数据量、`db.get_pool()` 真实路径 | PostgreSQL |
| 04 检索与嵌入 | BGE 嵌入维度与耗时、CrossEncoder 重排序、`fetch_laws`、`get_retriever().search`、RAG 工具链 | 模型缓存（首次冷启动 30-90s）+ PostgreSQL |
| 05 MCP 双向 | 三工具注册、stdio 握手、streamable-http 起服务与挂载、server 离线降级、adapters stdio 路径 | 无（自动挑空闲端口，不踩运行中的 server） |
| 06 端到端 | 按 `.env` 装配运行时并跑完整 graph，看是否产出答复 / 停在哪个 HITL | DEEPSEEK_API_KEY（会跑整图，数分钟） |
| 07 双模式 API | 起真实 uvicorn 子进程（Selector 循环）实测 `/disclaimer` / `/attorney/ask` / `/assistant/ask` / 双模式 SSE / 413·422·404 守卫 / `/sessions` 列表与详情 | DEEPSEEK_API_KEY + PostgreSQL（PG 不通时会话检查显式 SKIP） |

注：02、06、07 会消耗真实 LLM 配额；其余各册不调用 LLM（04 只加载本地模型）。

## 2026-09-18 实测状态（本机）

| 册 | 结论 | 要点 |
|---|---|---|
| 01 环境配置 | ALL PASSED | 24 PASS / 4 SKIP；`.env` 就位、语料 7 法条 + 11 案例、两个模型均已缓存 |
| 02 LLM 连通性 | HAS FAILURES | 普通对话与工具调用均可用；**`with_structured_output` 默认的 `json_schema` 被 API 拒（HTTP 400）**，`json_mode` 实测可用 |
| 03 PostgreSQL | HAS FAILURES | `postgres` 用户密码认证失败 → 库内所有表项 SKIP |
| 04 检索与嵌入 | HAS FAILURES | BGE(1024 维)/重排序 PASS；检索三项因库不通未验证 |
| 05 MCP 双向 | 5 PASS / 1 FAIL | 裸协议 stdio、streamable-http、离线降级全通；adapters 的 stdio 取工具在 ipykernel 下报 `fileno` |
| 06 端到端 | 未跑（依赖 02/03 的结论） | 见下「待修项」 |

### 待修项（按影响排序）

1. **`json_schema` 不可用**（**已修**：四个结构化节点已改 `method="json_mode"` 并在提示词内
   声明字段名，07 册实测三处 schema 链路可用；02 册的静态检查会按源码自动切换判定）。
2. **Postgres 认证失败** —— `.env` 的 `DB_PASSWORD` 仍是默认值。库不通则法条检索、案例检索、
   长期记忆、审计全部失效，checkpointer 降级 InMemory（进程重启丢会话）。
3. **语料未入库** —— `law_vector` / `law_cases` 空表；入库脚本 `scripts/ingest_cases_pgvector.py`
   在仓库中不存在（`base.py` 与 `pgvector_retriever.py` 的注释指向它）。
4. **`tests/test_mcp.py::test_stdio_end_to_end` 挂起未定位** —— 已排除 pytest 捕获模式
   （`--capture=no` 同样挂，>240s，子进程存活）与 stdio 入口本身（裸协议 3.2s 通），
   卡点在 `langchain-mcp-adapters` 的 stdio 取工具路径。

## 已知环境限制

- **ipykernel 下不能用 adapters 挂 stdio**：mcp sdk 的 `stdio_client` 默认 `errlog=sys.stderr`，
  Jupyter 的流没有文件描述符，报 `UnsupportedOperation: fileno`。裸协议探针显式传文件句柄即可绕开。
- **PostgreSQL 中文报错在 Windows 上显示为乱码**（`��������`），是驱动返回的消息编码问题，
  不影响判定，看 `type(e).__name__` 与 `SQLSTATE` 文本即可。
- notebook 里**不能用 `asyncio.run()`**（内核已在事件循环中），统一用顶层 `await`。
