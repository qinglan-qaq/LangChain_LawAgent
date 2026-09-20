# 子项目 C「工程地基」设计文档 — 配置中心 + JSON 日志 + 死字段清理 + 单例并发治理

- 日期：2026-09-11
- 状态：已与用户逐节确认（S1-S6 全部通过），待 spec 审阅
- 分支：upgrade-v1
- 上游：A1（LangGraph 1.x 升级）、A2（MCP 双向）、A3（问询与协同）已合入

## 1. 背景与定位

本项目为**求职/作品集展示项目**，本轮为三大改进方向的 **C 工程地基**（A 已完成，B 记忆系统后排）。基调：**架构可讲性优先，不做生产级运维配套**。

现状问题（均有代码出处，经全仓 grep 逐项验证）：

1. **运行时参数散落**：常量分布在 8 个文件（`state.py:127-128`、`rag_tools.py:134-136`、`LangGraph_lawApp.py:147` 等），env 读取点 25+。最危险的是**递归限制无任何配置**——走 LangGraph 默认 25，而 A3 扩图后最坏路径 ≈ 2(入口) + 10(澄清环) + 3×8(工具链) + ~6(重规划/兜底/finalize) ≈ 42 超步，复杂查询演示必翻车
2. **日志不可采集**：`FastAPI/logging.py` 五类结构化日志是管道分隔的人类可读格式（`_FILE_FMT`，`logging.py:74`），无法直接喂日志采集系统，"可观测性"故事缺一块
3. **AgentState 死字段**：9 个字段全链路（主图/FastAPI/SSE/notebook）只写不读；legacy `node/` 备用节点工厂目录**全仓零引用**；ingest 存在隐性错值（`crag_context` 被塞 `EvaluationResult()` 默认对象，`LangGraph_lawApp.py:235`，疑似复制粘贴残留）
4. **懒加载单例无并发保护**：planner/executor LLM（`LangGraph_lawApp.py:97/113`）、分析 LLM（`rag_tools.py:36`）、embedder/reranker（`embedder.py:25/36`）、pinecone 服务（`pinecone_retriever.py:23`）均为 `global _x` 无锁初始化
5. **文档滞后**：PROJECT_OVERVIEW §12 的 BM25 路径条目已过时（`RAG_program.py:88` 实际已是 `os.path.dirname(__file__)` + `BM25_PATH` 覆盖）；阈值写的 0.7/0.3 与代码实际 0.5/0.2（`rag_tools.py:134`）不符

## 2. 已确认决策记录

| # | 决策点 | 结论 |
|---|--------|------|
| 1 | 优先方向 | C 工程地基先行，B 记忆系统后排 |
| 2 | 基调 | 作品集优先：并发用轻量手段（锁），不做连接池/健康检查等运维配套 |
| 3 | 本期条目 | (1)递归限制+阈值配置化 (2)日志 JSON 化 (3)AgentState 死字段清理；**工具返回 pydantic schema 化进 backlog** |
| 4 | 测试策略 | pytest 为主（新增行为断言）+ A 的 3 个 notebook 复跑回归 |
| 5 | 配置范围 | 全量收拢：全部运行时常量与 env 点进 config.py 单一入口 |
| 6 | 实现方案 | 渐进式单一 spec：config → 日志 → 死字段 → 并发锁 → 文档，风险递增 |
| 7 | legacy node/ | **整目录删除**（含 `langgraph_nodes.py` + `nodes_test.ipynb`，零引用已验证） |
| 8 | 阈值默认值 | 沿用代码现值 0.5/0.2/3，同步修正文档错误 |
| 9 | 设计文档 | 表格优先，代码样例仅关键机制 |

## 3. 目标 / 非目标

**目标**

- G1 配置中心：`config.py` 单一入口，pydantic-settings 类型安全，env 同名覆盖，`RECURSION_LIMIT=60` 防演示翻车
- G2 JSON 文件日志：文件 handler 输出 JSON 行（含结构化 detail/result），控制台保持人类可读
- G3 地基清理：删除 9 个死字段 + legacy `node/` 目录，`_STATE_KEYS` 收缩，消除 ingest 隐性错值
- G4 单例并发治理：5 处懒加载单例加 `threading.Lock` 双检
- G5 文档同步：§12/§13 修正、配置一览表、README 核对

**非目标（backlog / 留给 B）**

- 工具返回 pydantic schema 化（本期明确不做，进 backlog）
- B 记忆系统（短期压缩/长期固化）
- 角色切换会话级（`LEGAL_ANALYSIS_ROLE` 保持 env 级）
- 连接池、健康检查、日志采集接入等生产运维配套
- MCP 侧改动、前端页面

## 4. 配置中心设计（新建 `lawApp_LangGraph/config.py`）

### 4.1 形态

`pydantic_settings.BaseSettings`（环境已有 2.15.0，零新依赖）+ 模块级 `settings` 单例。**env 无前缀同名覆盖**（如 `RECURSION_LIMIT=80`），支持 `.env` 文件，`extra="ignore"` 容忍无关 env。

```python
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 图执行
    recursion_limit: int = 60
    max_rounds: int = 10
    max_clarify_rounds: int = 5
    error_streak_threshold: int = 2
    # 评估阈值（沿用代码现值）
    correct_threshold: float = 0.5
    incorrect_threshold: float = 0.2
    min_quality_docs: int = 3
    # ... 其余分组见 4.2

settings = Settings()
```

### 4.2 字段总表

| 分组 | 字段 | 默认值 | 原位置 |
|------|------|--------|--------|
| 图执行 | `recursion_limit` | **60**（新增，依据：A3 最坏路径 ≈42 超步 + 余量） | 无（走 LangGraph 默认 25） |
| 图执行 | `max_rounds` | 10 | `LangGraph_lawApp.py:147` |
| 图执行 | `max_clarify_rounds` | 5 | `state.py:127` |
| 图执行 | `error_streak_threshold` | 2 | `state.py:128` |
| 评估阈值 | `correct_threshold` / `incorrect_threshold` / `min_quality_docs` | 0.5 / 0.2 / 3 | `rag_tools.py:134-136` |
| LLM | `deepseek_pro_model` / `deepseek_flash_model` / `deepseek_model` | deepseek-reasoner / deepseek-chat / deepseek-chat | 各 getenv 点 |
| LLM | `deepseek_api_key` / `deepseek_base_url` | "" / https://api.deepseek.com | 各 getenv 点 |
| 检索 | `embed_model` / `embed_dim` / `rerank_model` / `rerank_enabled` | BAAI/bge-large-zh-v1.5 / 1024 / BAAI/bge-reranker-large / 1 | `embedder.py:19-21` |
| 检索 | `bm25_path` | None（未设时 `RAG_program` 以模块相对路径兜底，行为与现状一致） | `RAG_program.py:88` |
| 日志 | `log_dir` / `log_console_level` / `log_file_level` | logs / INFO / DEBUG | `FastAPI/logging.py` |
| MCP | `mcp_server_url` / `mcp_host` / `mcp_port` / `mcp_tools_enabled` | http://127.0.0.1:9381/mcp / 127.0.0.1 / 9381 / 1 | `mcp_client.py` 等 |
| DB | `db_host` / `db_port` / `db_name` / `db_user` / `db_password` / `db_pool_max` / `database_url` / `checkpoint_backend` | 现 getenv 缺省 | `db.py` / `runtime.py` |

**明确不收拢**：LLM 温度与 max_tokens（`LangGraph_lawApp.py:105-106/119-120`）属提示词工程参数，非运维配置，保持字面量；`HF_ENDPOINT`（模型下载镜像，进程启动期生效）保持原位。

### 4.3 切换方式与边界

- 原位置的模块常量与 `os.getenv` **全部删除**，消费点直接 `from lawApp_LangGraph.config import settings` 后 `settings.X`；**不保留 re-export 别名**（避免两个入口的假象），pytest/notebook 中的常量断言就地更新（沿用 A 的"存量最小更新"惯例，预计涉及 `test_smoke.py` 的 `MAX_CLARIFY_ROUNDS`/`ERROR_STREAK_THRESHOLD` import 与 3 个 notebook 的阈值引用）
- `recursion_limit` 挂接点：`FastAPI/utils.py:graph_config()` 返回值追加 `"recursion_limit": settings.recursion_limit`（`/ask`、`/ask/resume`、SSE 流全部经此函数构造 config，单一挂接点全覆盖）
- `bm25_path` 默认值在 config 内做"相对于 `RAG_service/` 模块"的路径解析，`BM25_PATH` env 语义不变
- 测试用 `monkeypatch.setenv` + 临时 `Settings()` 实例断言覆盖行为，**不 reload 全局单例**

## 5. 日志 JSON 化（`FastAPI/logging.py`）

只动 Formatter 层，5 类 logger（AgentFlow/AgentDebug/Tool/RAG/System）与 `_BaseLogger` 接口不动：

- 新增 `_JsonFormatter(logging.Formatter)`：输出 JSON 行，字段 `{"ts", "level", "session", "logger", "msg", "detail", "result"}`；现有 `detail=`/`result=` 结构化参数（`_BaseLogger` 打进 `record`）序列化进对应字段，无则省略
- **文件 handler** 换用 `_JsonFormatter`（`logs/agent_flow.log` 等变 JSON 行）；**控制台 handler 保留**现有 `_AgentFormatter` 彩色人类可读
- `log_dir`/`log_console_level`/`log_file_level` 三个 getenv 点收进 config（§4.2）
- 日志样例（验收参照）：

```json
{"ts": "2026-09-11T10:30:00", "level": "INFO", "session": "s-abc123", "logger": "agent_flow", "msg": "← Element Assess 完成", "detail": "known=3/7 | critical_missing=0", "result": "elapsed=0.42s | 放行"}
```

## 6. 死字段清理（AgentState + legacy node/）

### 6.1 消费矩阵（全链路证据：主图 + FastAPI + SSE 载荷 + 3 个 notebook + legacy node/）

| 处置 | 字段 | 证据 |
|------|------|------|
| **删除 ×9** | `session_id` | 全链路零写入零读取（FastAPI 的同名变量是本地参数，非 state 消费） |
| | `user_id` | 仅 `db.py` 自身函数参数同名，state 层零读写 |
| | `is_law_questions` / `is_simple_questions` / `should_continue` | 仅被零引用的 legacy `node/langgraph_nodes.py` 消费 |
| | `crag_context` | 仅 ingest 重置（且被塞 `EvaluationResult()` 错值）+ merge 合并，无读取 |
| | `memory_results` / `memory_update` | 工具返回写入 + ingest 重置，无读取（信息已由 `tool_calls` 记录与 messages 保留） |
| | `is_pdf_output` | `tools.py:172/180` 写入 + ingest 重置，无读取（PDF 确认流走 `pdf_confirmed`/`pdf_path`） |
| **保留 ×4**（候选中存活） | `final_prompts` | `api.py`/`utils.py` 消费 |
| | `pdf_path` | `api.py` 消费 |
| | `reasoning` | API 载荷（`model.py`/`utils.py`）+ 3 notebook 断言 |
| | `hitl_event` | `hitl_test.ipynb` 断言 + 审计链路（B 消费预留） |
| **结构保留** | `case_elements` / `clarify_history` 等 A3 字段 | B 记忆固化原料（A spec §11 明确预留） |

### 6.2 连带动作

1. `state.py` AgentState 删除 9 字段定义；`ingest_node` 的 RESET/重置字典同步删除对应行（`crag_context` 错值随之消失）
2. `LangGraph_lawApp.py` `_STATE_KEYS` 收缩：去掉 `crag_context` / `memory_results` / `memory_update` / `is_pdf_output`（其余键保留）
3. **工具返回字典不动**：`memory_results`/`memory_update`/`is_pdf_output` 等键保留在 `db_tools.py`/`tools.py` 的返回中——它们作为 ToolMessage 内容仍是执行器 LLM 的上下文，仅因 `_STATE_KEYS` 收缩不再合入 state（对 LLM 可见性不变，merge 行为最小变更）
4. **删除 `node/` 整目录**（`langgraph_nodes.py` + `nodes_test.ipynb`，全仓零引用）
5. 受影响断言就地更新：`test_smoke.py` 无死字段直接断言（已验证），notebook 若有引用随回归修复

## 7. 单例并发治理（轻量）

5 处懒加载单例统一加 `threading.Lock` 双检初始化，模式：

```python
_lock = threading.Lock()
_service = None

def get_xxx():
    global _service
    if _service is None:
        with _lock:
            if _service is None:
                _service = _make()
    return _service
```

适用点：`get_planner_llm` / `get_executor_llm`（`LangGraph_lawApp.py:97/113`）、`rag_tools._get_llm`、`embedder.get_embedder` / `get_reranker`、`pinecone_retriever` 服务初始化。说明：FastAPI 异步单事件循环本身无竞争，锁是防 uvicorn 多 worker/多线程演示场景；文档注明作品集定位下不做连接池。

## 8. 文档同步

- PROJECT_OVERVIEW §12 重写：BM25 条目改为"已修复（v3.1 已用模块相对路径 + env 覆盖）"移出待办；阈值条目修正为实际值 0.5/0.2/3 + "已配置化"；递归限制条目改为"已配置化（RECURSION_LIMIT 默认 60）"；并发条目改为"单例已加锁，连接池仍留生产化"
- §13 目录结构：删 `node/`，新增 `config.py`
- 新增「配置一览表」小节（§14 前）：env 名 / 默认值 / 说明
- README 工程亮点表核对（16 用例数、目录、特性描述）
- 环境变量清单更新（`README.md` / PROJECT_OVERVIEW 部署节）

## 9. 测试方案（pytest 为主 + notebook 回归）

新增 4 组用例，独立文件 `tests/test_engineering.py`（与存量冒烟分离，命名对齐"工程"主题）：

| 用例 | 断言 |
|------|------|
| `test_config_defaults_and_env` | 全部字段默认值；`monkeypatch.setenv("RECURSION_LIMIT", "99")` 后 `Settings().recursion_limit == 99`；非法值（`"abc"`）抛 `ValidationError` |
| `test_json_logging` | 文件 handler 输出行可 `json.loads`，含 ts/level/session/logger/msg；`debug.info(..., detail=...)` 后 detail 字段存在 |
| `test_state_dead_fields_removed` | `AgentState` 无 9 个死字段（`hasattr` 全 False）；`final_prompts/pdf_path/reasoning/hitl_event` 仍在 |
| `test_graph_config_recursion` | `graph_config("s1")` 含 `recursion_limit == settings.recursion_limit` |

存量更新：`test_smoke.py` 中 `MAX_CLARIFY_ROUNDS`/`ERROR_STREAK_THRESHOLD` import 改从 `config.settings` 取值（或断言改字面量）；节点计数/断言不涉及死字段则不动。

回归：A 的 3 个 notebook（clarify/hitl/prompts）以 `scripts/run_nb.py` 复跑全通过，作为全链路行为不变的验收。

## 10. 交付物清单

| 文件 | 变更 |
|------|------|
| `lawApp_LangGraph/config.py` | **新建**：Settings 全量字段 + settings 单例 |
| `lawApp_LangGraph/state.py` | 删 2 常量与 9 死字段；改引 settings |
| `lawApp_LangGraph/LangGraph_lawApp.py` | 删 `MAX_ROUNDS` 常量、`_STATE_KEYS` 收缩、ingest 重置行清理；LLM 装配改引 settings + 加锁 |
| `lawApp_LangGraph/tools/rag_tools.py` | 删 3 阈值常量改引 settings；`_get_llm` 加锁 |
| `lawApp_LangGraph/RAG_service/embedder.py` / `pinecone_retriever.py` / `RAG_program.py` | 常量改引 settings；单例加锁 |
| `lawApp_LangGraph/node/` | **整目录删除** |
| `lawApp_LangGraph/FastAPI/logging.py` | `_JsonFormatter` + 文件 handler 切换；级别常量改引 settings |
| `lawApp_LangGraph/FastAPI/utils.py` | `graph_config` 挂 recursion_limit |
| `lawApp_LangGraph/FastAPI/api.py` / `db.py` / `mcp_client.py` / `runtime.py` | getenv 点改引 settings |
| `tests/test_engineering.py` | **新建**：4 组用例 |
| `tests/test_smoke.py` | 常量 import 就地更新 |
| 3 个 A notebook | 常量引用随回归更新（如有） |
| `lawApp_LangGraph/PROJECT_OVERVIEW.md` / `README.md` | §8 全部文档同步项 |

## 11. 风险与边界情况

| 风险 | 缓解 |
|------|------|
| env 覆盖测试污染全局单例 | 测试只构造临时 `Settings()` 实例，不 reload `settings` 单例 |
| `.env` 与 `load_dotenv()` 双重加载顺序 | config 的 `env_file` 优先级低于进程 env（pydantic-settings 语义），与现有 `load_dotenv` 共存无冲突；计划中明确验证一次 |
| 死字段删除破坏 checkpointer 历史会话 | upgrade-v1 开发期无线上存量，直接删不加迁移（与 A3 决策一致） |
| `node/` 删除影响 | 零引用已验证；`nodes_test.ipynb` 随目录删除，不迁移 |
| recursion_limit 提高放大失控循环成本 | `MAX_ROUNDS=10` 仍是工具调用硬闸；两者职责正交（超步数 vs 工具预算） |
| JSON 日志破坏现有 grep 习惯 | 控制台格式不变；文件日志消费者仅人（无脚本依赖，已验证） |
| `_STATE_KEYS` 收缩后工具结果不再入 state | 工具返回字典保持原样，仍经 ToolMessage 进入 LLM 上下文；`_step_summaries` 消费的是 state 存活字段，已验证无交叉 |

## 12. 与 B 的接口预留

- config 的 DB/记忆配置分组即 B 记忆系统的连接配置入口
- `hitl_event` 审计链路保留（B 直接消费）
- 本期不动的 `search_memory`/`save_to_memory` 工具与 `case_elements`/`clarify_history` 原料均不受死字段清理影响
