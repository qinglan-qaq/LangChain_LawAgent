# 子项目C「工程地基」实现计划 — 配置中心 + JSON 日志 + 死字段清理 + 单例并发治理

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `docs/superpowers/specs/2026-09-11-engineering-foundation-design.md` 落地：pydantic-settings 配置中心（含 `RECURSION_LIMIT`）、文件日志 JSON 化、AgentState 9 个死字段与 legacy `node/` 目录清理、6 处懒加载单例加锁、文档同步。

**Architecture:** 新建 `lawApp_LangGraph/config.py` 作为唯一配置入口，全仓 `os.getenv` 与散落常量切换为 `settings.X` 引用；日志只动 Formatter 层；死字段删除保持工具返回字典不动（ToolMessage 上下文价值）。行为不变约束：所有默认值沿用代码现值。

**Tech Stack:** Python 3.11 / pydantic-settings 2.15.0（已在环境，零新依赖）/ pytest（主验证）+ nbclient（回归）

## Global Constraints

- Python 解释器一律用 `/Users/qinglan/miniconda3/envs/lawagent/bin/python`（下文简写 `$PY`）
- **零新依赖**：pydantic-settings 2.15.0 已在 lawagent 环境
- **行为不变**：所有配置默认值 = 代码现值（阈值 0.5/0.2/3、DB 端口 5432 等），只增加 env 覆盖能力；死字段删除是唯一的行为变更（删除本身）
- **不收拢**：LLM 温度与 max_tokens（提示词工程参数，保持字面量）、`HF_ENDPOINT`（`RAG_program.py:24-25` 启动期生效，保持原位）
- **不保留 re-export 别名**：原常量（`MAX_ROUNDS`/`MAX_CLARIFY_ROUNDS`/`ERROR_STREAK_THRESHOLD`/`CORRECT_THRESHOLD` 等）删除后不再以任何形式重导出，消费点直接 `settings.X`
- **测试策略**：pytest 为主（新文件 `tests/test_engineering.py`），存量 `test_smoke.py` 只做被迫的最小更新；收尾以 `scripts/run_nb.py` 复跑 A 的 3 个 notebook 作全链路回归
- 代码风格沿用 A3 约定：**禁止横线分隔注释**（任何 `# ---` 类画线）；新增/改写的 docstring 用 **Google 风格**；节点纯记账不调 LLM 等既有风格不动
- 每个任务结束必须 `git commit`，commit message 用中文、格式仿照 `A3: xxx — ...`，本系列前缀 `C:`

### 执行方式与代码风格约束（执行要求，优先级高于任务内代码样例）

- **执行方式：子代理**。每个任务由控制器派发一个全新的实现子代理 + 一个独立的评审子代理，控制器只做协调、裁决与台账记录；子代理不继承会话历史，只接收任务简报、接口上下文与本节约束。
- **高内聚、低耦合**。config.py 只依赖 pydantic-settings，不 import 项目内任何模块（避免环）；其他模块对配置的依赖只经 `from lawApp_LangGraph.config import settings` 一条路径。
- **禁横线分隔注释**；Google 风格多行 docstring（`Args:`/`Returns:`/`Raises:`/`Example:`）。
- **settings 单例语义**：`settings = Settings()` 在 import 期求值一次；测试中验证 env 覆盖一律构造临时 `Settings(_env_file=None)` 实例，**绝不 reload 全局单例**。存量测试经查不 monkeypatch 环境变量（`no_llm` fixture 用 `monkeypatch.setattr` 打函数），无切换陷阱。

## File Structure（全局地图）

```
lawApp_LangGraph/
├── config.py                    # Task 1: 新建，配置中心
├── state.py                     # Task 2: 删 2 常量改引 settings; Task 6: 删 9 死字段
├── LangGraph_lawApp.py          # Task 2: 常量切换; Task 4: LLM 单例加锁; Task 6: ingest/_STATE_KEYS 清理
├── tools/rag_tools.py          # Task 2: 阈值切换; Task 4: _get_llm 加锁
├── RAG_service/embedder.py      # Task 3: 常量切换; Task 4: 加锁
├── RAG_service/pinecone_retriever.py  # Task 3: Pinecone env 切换; Task 4: 加锁
├── RAG_service/RAG_program.py   # Task 3: BM25_PATH 切换
├── db.py / runtime.py / mcp_client.py / mcp_server.py  # Task 3: env 切换
├── FastAPI/logging.py           # Task 5: _JsonFormatter + force 参数
├── FastAPI/utils.py             # Task 2: graph_config 挂 recursion_limit
├── FastAPI/api.py               # Task 3: lifespan 日志参数切 settings
├── node/                        # Task 6: 整目录删除
├── clarify_test.ipynb           # Task 7: 常量引用更新
├── hitl_test.ipynb              # Task 7: 常量引用更新
├── prompts_test.ipynb           # Task 7: 复跑回归（无引用，已验证）
└── PROJECT_OVERVIEW.md          # Task 8: 文档同步
tests/test_engineering.py        # Task 1/2/5/6: 逐任务新增用例
tests/test_smoke.py             # Task 2: 常量 import 就地更新
README.md                        # Task 8: 核对同步
```

依赖顺序：Task 1（config 基座）→ 2（图执行切换）→ 3（外围切换）→ 4（单例锁）→ 5（JSON 日志）→ 6（死字段+legacy 删除）→ 7（notebook 更新+回归）→ 8（文档）。风险递增：配置类改动在前，删除类改动在后，每步全量 pytest 兜底。

---

### Task 1: 配置中心 — config.py + test_engineering.py 骨架

**Files:**
- Create: `lawApp_LangGraph/config.py`
- Create: `tests/test_engineering.py`

**Interfaces:**
- Produces（后续所有任务依赖）:
  - `from lawApp_LangGraph.config import settings` —— 模块级单例，字段清单见下方完整代码
  - `Settings` 类（测试用，可 `Settings(_env_file=None)` 构造临时实例）
  - 字段命名与 env 名一一对应（pydantic-settings 大写匹配）：`recursion_limit`↔`RECURSION_LIMIT`、`memory_embed_model`↔`MEMORY_EMBED_MODEL` 等
  - key 类字段（`deepseek_api_key`/`pinecone_api_key`/`database_url`/`bm25_path`）为 `Optional[str] = None`，保持 `os.getenv` 无值时返回 None 的现语义

- [ ] **Step 1: 写失败测试**

`tests/test_engineering.py`：

```python
"""工程地基测试 — 配置中心 / JSON 日志 / 死字段清理 / 递归限制。

运行: /Users/qinglan/miniconda3/envs/lawagent/bin/python -m pytest tests/ -q
设计来源: docs/superpowers/specs/2026-09-11-engineering-foundation-design.md
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_config_defaults_and_env(monkeypatch):
    """默认值与 env 覆盖; 临时实例隔离, 不动全局单例。"""
    from lawApp_LangGraph.config import Settings, settings

    s = Settings(_env_file=None)
    # 图执行
    assert s.recursion_limit == 60
    assert s.max_rounds == 10
    assert s.max_clarify_rounds == 5
    assert s.error_streak_threshold == 2
    # 评估阈值(代码现值)
    assert (s.correct_threshold, s.incorrect_threshold, s.min_quality_docs) == (
        0.5, 0.2, 3,
    )
    # LLM / 检索 / DB 抽查
    assert s.deepseek_pro_model == "deepseek-reasoner"
    assert s.deepseek_flash_model == "deepseek-chat"
    assert s.memory_embed_model == "BAAI/bge-large-zh-v1.5"
    assert s.embed_dim == 1024
    assert s.bm25_path is None
    assert s.database_url is None
    assert s.db_port == 5432
    # 全局单例存在且类型正确
    assert isinstance(settings, Settings)

    # env 覆盖
    monkeypatch.setenv("RECURSION_LIMIT", "99")
    monkeypatch.setenv("MIN_QUALITY_DOCS", "7")
    monkeypatch.setenv("DEEPSEEK_FLASH_MODEL", "my-flash")
    s2 = Settings(_env_file=None)
    assert s2.recursion_limit == 99
    assert s2.min_quality_docs == 7
    assert s2.deepseek_flash_model == "my-flash"

    # 非法值类型校验
    monkeypatch.setenv("RECURSION_LIMIT", "abc")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
```

- [ ] **Step 2: 运行验证失败**

Run: `$PY -m pytest tests/test_engineering.py -v`
Expected: FAIL / ERROR（ModuleNotFoundError: No module named 'lawApp_LangGraph.config'）

- [ ] **Step 3: 写 config.py**

```python
"""运行时配置中心 — 全部调参与环境变量的单一入口。

字段与同名环境变量(大写)一一对应, 优先级: 进程 env > .env 文件 > 默认值。
设计来源: docs/superpowers/specs/2026-09-11-engineering-foundation-design.md

明确不收拢(见 spec §4.3):
    LLM 温度/max_tokens  — 提示词工程参数, 保持代码字面量
    HF_ENDPOINT          — 进程启动期生效, 留在 RAG_program.py
"""

from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全量运行时配置。

   env 同名覆盖示例: RECURSION_LIMIT=80 即生效, 无需改代码。
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ============ 图执行 ============
    # LangGraph 超步上限(A3 最坏路径约 42 超步, 60 留余量)
    recursion_limit: int = 60
    # 工具调用总数上限, 防无限重规划
    max_rounds: int = 10
    # 入口澄清轮数上限
    max_clarify_rounds: int = 5
    # 连续失败触发降级询问的阈值
    error_streak_threshold: int = 2

    # ============ 评估阈值(CRAG 三档) ============
    correct_threshold: float = 0.5
    incorrect_threshold: float = 0.2
    min_quality_docs: int = 3

    # ============ LLM ============
    deepseek_pro_model: str = "deepseek-reasoner"
    deepseek_flash_model: str = "deepseek-chat"
    deepseek_api_key: Optional[str] = None
    deepseek_base_url: str = "https://api.deepseek.com"

    # ============ 检索: 嵌入 / 重排序 / BM25 ============
    # env 名为 MEMORY_EMBED_MODEL(历史命名, 与 db_tools 记忆索引共用)
    memory_embed_model: str = "BAAI/bge-large-zh-v1.5"
    rerank_model: str = "BAAI/bge-reranker-large"
    embed_dim: int = 1024
    # "0" 禁用(保持原字符串语义, 非 bool)
    rerank_enabled: str = "1"
    # None 时 RAG_program 以模块相对路径兜底
    bm25_path: Optional[str] = None

    # ============ 检索: Pinecone ============
    pinecone_index_name: str = "pinecone-test-lawapp"
    pinecone_api_key: Optional[str] = None
    pinecone_cloud: str = "aws"
    pinecone_region: str = "us-east-1"

    # ============ 日志 ============
    log_dir: str = "./logs"
    log_console_level: str = "DEBUG"
    log_file_level: str = "INFO"

    # ============ MCP ============
    mcp_server_url: str = "http://127.0.0.1:9381/mcp"
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 9381
    # "0"/"false"/"no" 禁用(保持原字符串语义, 非 bool)
    mcp_tools_enabled: str = "1"

    # ============ DB / 持久化 ============
    checkpoint_backend: str = "auto"
    database_url: Optional[str] = None
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "Law_app"
    db_user: str = "postgres"
    db_password: str = "postgres"
    db_pool_max: int = 10


settings = Settings()
```

注意：docstring 里 `"""全量运行时配置。\n\n   env 同名覆盖示例...` 首行后接内容需符合 Google 风格——落地时按本仓现有模块 docstring 形态微调（行为不变）。

- [ ] **Step 4: 运行验证通过**

Run: `$PY -m pytest tests/test_engineering.py -v`
Expected: 1 passed

- [ ] **Step 5: 全量 pytest 确认存量不受影响**

Run: `$PY -m pytest tests/ -q`
Expected: 17 passed（16 存量 + 1 新增）

- [ ] **Step 6: Commit**

```bash
git add lawApp_LangGraph/config.py tests/test_engineering.py
git commit -m "C: 配置中心 — config.py 单一入口(全量字段+env同名覆盖) + 工程测试骨架"
```

---

### Task 2: 图执行切换 — 常量删除 + graph_config 挂 recursion_limit

**Files:**
- Modify: `lawApp_LangGraph/FastAPI/utils.py:26-27`
- Modify: `lawApp_LangGraph/state.py:127-128`
- Modify: `lawApp_LangGraph/LangGraph_lawApp.py:58-70,147,1421` 及全文常量引用
- Modify: `lawApp_LangGraph/tools/rag_tools.py:134-136,204,206,214`
- Modify: `tests/test_smoke.py:148-184`
- Test: `tests/test_engineering.py`（追加用例）

**Interfaces:**
- Consumes: Task 1 的 `settings`
- Produces:
  - `graph_config(session_id: str) -> dict` 返回值含 `"recursion_limit": int`（`/ask`、`/ask/resume`、SSE 全部经此函数构造 config，单一挂接点全覆盖）
  - `state.py` 不再定义 `MAX_CLARIFY_ROUNDS`/`ERROR_STREAK_THRESHOLD`；`LangGraph_lawApp.py` 不再定义 `MAX_ROUNDS`；`rag_tools.py` 不再定义三档阈值常量

- [ ] **Step 1: 写失败测试（追加到 test_engineering.py 末尾）**

```python
def test_graph_config_recursion():
    """graph_config 携带 recursion_limit, 与 settings 单一来源。"""
    from lawApp_LangGraph.FastAPI.utils import graph_config
    from lawApp_LangGraph.config import settings

    cfg = graph_config("s-r1")
    assert cfg["configurable"]["thread_id"] == "s-r1"
    assert cfg["recursion_limit"] == settings.recursion_limit
```

- [ ] **Step 2: 运行验证失败**

Run: `$PY -m pytest tests/test_engineering.py::test_graph_config_recursion -v`
Expected: FAIL（KeyError: 'recursion_limit'）

- [ ] **Step 3: 改 utils.py 的 graph_config**

```python
def graph_config(session_id: str) -> dict:
    return {
        "configurable": {"thread_id": session_id},
        "recursion_limit": settings.recursion_limit,
    }
```

`utils.py` 顶部加 `from lawApp_LangGraph.config import settings`。

- [ ] **Step 4: 运行验证通过**

Run: `$PY -m pytest tests/test_engineering.py::test_graph_config_recursion -v`
Expected: PASS

- [ ] **Step 5: 切换 state.py 常量**

删除 `state.py:127-128` 两行（`MAX_CLARIFY_ROUNDS = 5` / `ERROR_STREAK_THRESHOLD = 2`）；`AgentState` 内引用常量名的注释（338、343 行附近）措辞同步改为 `settings.max_clarify_rounds` / `settings.error_streak_threshold`。

- [ ] **Step 6: 切换 LangGraph_lawApp.py**

1. import 块（58-70 行）删 `ERROR_STREAK_THRESHOLD,` 与 `MAX_CLARIFY_ROUNDS,` 两行；在 `load_dotenv()` 之后加：

```python
from lawApp_LangGraph.config import settings
```

2. 删除 147 行 `MAX_ROUNDS = 10  # 工具调用总数上限，防无限重规划`
3. 全文替换（含注释措辞）：
   - `MAX_CLARIFY_ROUNDS` → `settings.max_clarify_rounds`（使用点：349、369、397、442、456、470、1333、1345 及文件头 10、32、430 行注释）
   - `ERROR_STREAK_THRESHOLD` → `settings.error_streak_threshold`（使用点：1370、1394）
   - 1421 行 `if executed >= MAX_ROUNDS:` → `if executed >= settings.max_rounds:`（1411 行注释同步）

- [ ] **Step 7: 切换 rag_tools.py 阈值**

1. 删除 134-136 行三行常量（`CORRECT_THRESHOLD`/`INCORRECT_THRESHOLD`/`MIN_QUALITY_DOCS`）
2. 顶部加 `from lawApp_LangGraph.config import settings`
3. 204 行 `if score >= CORRECT_THRESHOLD:` → `settings.correct_threshold`；206 行同理 `settings.incorrect_threshold`；214 行 `if len(correct) >= MIN_QUALITY_DOCS or total_usable >= MIN_QUALITY_DOCS:` → 两处 `settings.min_quality_docs`

- [ ] **Step 8: 更新 test_smoke.py 常量断言**

`test_case_elements_model`（148 行起）的两处修改：

```python
def test_case_elements_model():
    from lawApp_LangGraph.state import CaseElements, default_case_elements
    from lawApp_LangGraph.config import settings
```

（import 行去掉 `MAX_CLARIFY_ROUNDS, ERROR_STREAK_THRESHOLD`）以及末尾断言：

```python
    assert settings.max_clarify_rounds == 5
    assert settings.error_streak_threshold == 2
```

- [ ] **Step 9: 全量 pytest**

Run: `$PY -m pytest tests/ -q`
Expected: **18 passed**（16 存量 + 本任务新增 2：test_config_defaults_and_env 已在 Task 1 计入；本任务净新增 test_graph_config_recursion 1 个，即 16+1+1=18）

- [ ] **Step 10: Commit**

```bash
git add lawApp_LangGraph/FastAPI/utils.py lawApp_LangGraph/state.py lawApp_LangGraph/LangGraph_lawApp.py lawApp_LangGraph/tools/rag_tools.py tests/test_smoke.py tests/test_engineering.py
git commit -m "C: 图执行切换 — 常量删除改引settings + graph_config挂recursion_limit(默认60)"
```

---

### Task 3: 外围模块切换 — LLM/检索/DB/MCP/日志参数

**Files:**
- Modify: `lawApp_LangGraph/LangGraph_lawApp.py:97-121`（get_planner_llm/get_executor_llm 的 env）
- Modify: `lawApp_LangGraph/tools/rag_tools.py:36-50`（_get_llm 的 env）
- Modify: `lawApp_LangGraph/RAG_service/embedder.py:19-21,29,38`
- Modify: `lawApp_LangGraph/RAG_service/pinecone_retriever.py:23-34`
- Modify: `lawApp_LangGraph/RAG_service/RAG_program.py:88-90`
- Modify: `lawApp_LangGraph/db.py:31-52`
- Modify: `lawApp_LangGraph/runtime.py:49`
- Modify: `lawApp_LangGraph/mcp_client.py:28,46`
- Modify: `lawApp_LangGraph/mcp_server.py:181-182`
- Modify: `lawApp_LangGraph/FastAPI/api.py:57-61`

**Interfaces:**
- Consumes: Task 1 的 `settings`
- Produces: 全仓 `os.getenv` 仅剩 spec 豁免项（`HF_ENDPOINT`，`RAG_program.py:24`）；`mcp_client.py` 的模块常量 `MCP_SERVER_URL` 删除

- [ ] **Step 1: LLM 装配切换**

`LangGraph_lawApp.py` 两个函数（settings import 已在 Task 2 加好）：

```python
        _llm_planner = ChatOpenAI(
            model=settings.deepseek_pro_model,
            temperature=0.4,
            max_tokens=4096,
            openai_api_key=settings.deepseek_api_key,
            openai_api_base=settings.deepseek_base_url,
        )
```

```python
        _llm_executor = ChatOpenAI(
            model=settings.deepseek_flash_model,
            temperature=0.25,
            max_tokens=2048,
            openai_api_key=settings.deepseek_api_key,
            openai_api_base=settings.deepseek_base_url,
        )
```

（温度/max_tokens 字面量不动——Global Constraints 豁免项。）

`rag_tools.py` `_get_llm` 同理：`model=settings.deepseek_flash_model`、`openai_api_key=settings.deepseek_api_key`、`openai_api_base=settings.deepseek_base_url`（温度 0.4/max_tokens 4096 不动）。

- [ ] **Step 2: embedder.py 切换**

删除 19-21 行三个常量（`EMBED_MODEL`/`RERANK_MODEL`/`EMBED_DIM`），顶部加 `from lawApp_LangGraph.config import settings`：

```python
def get_embedder():
    """SentenceTransformer BGE 嵌入模型（懒加载单例）。"""
    global _embedder
    if _embedder is None:
        from sentence_transformers import SentenceTransformer

        logger.info("初始化 Embedder (冷启动) | model=%s", settings.memory_embed_model)
        _embedder = SentenceTransformer(settings.memory_embed_model)
    return _embedder


def get_reranker():
    """CrossEncoder 重排序模型（懒加载单例，RERANK_ENABLED=0 可禁用）。"""
    global _reranker
    if _reranker is None:
        if settings.rerank_enabled == "0":
            return None
        from sentence_transformers import CrossEncoder

        logger.info("初始化 CrossEncoder (冷启动) | model=%s", settings.rerank_model)
        _reranker = CrossEncoder(settings.rerank_model, max_length=512)
    return _reranker
```

`embedder.py` 中 `EMBED_DIM` 的其他使用点（如有，grep `EMBED_DIM`）同样替换为 `settings.embed_dim`。

- [ ] **Step 3: pinecone_retriever.py / RAG_program.py 切换**

`_get_service` 内四个 env 改 `settings.pinecone_index_name` / `settings.pinecone_api_key` / `settings.pinecone_cloud` / `settings.pinecone_region`（顶部加 import；`# type: ignore[arg-type]` 注释随 api_key 变 None 语义保留）。

`RAG_program.py:88` 改：

```python
        bm25_path = settings.bm25_path or os.path.join(
            os.path.dirname(__file__), "bm25_law_params.json"
        )
```

（顶部加 import；`os` 若仍被别处使用则保留 import。）

- [ ] **Step 4: db.py / runtime.py / mcp 切换**

`db.py`（31-52 行）：`DATABASE_URL`→`settings.database_url`、`DB_USER`→`settings.db_user`、`DB_PASSWORD`→`settings.db_password`、`DB_HOST`→`settings.db_host`、`DB_PORT`→`settings.db_port`、`DB_NAME`→`settings.db_name`、`DB_POOL_MAX`→`settings.db_pool_max`（`int(...)` 包装随 int 字段删除）。

`runtime.py:49`：`os.getenv("CHECKPOINT_BACKEND", "auto").lower()` → `settings.checkpoint_backend.lower()`。

`mcp_client.py`：删除 28 行 `MCP_SERVER_URL = ...` 模块常量，使用点改 `settings.mcp_server_url`（grep `MCP_SERVER_URL` 全部替换）；46 行 `os.getenv("MCP_TOOLS_ENABLED", "1").lower() in ("0", "false", "no")` → `settings.mcp_tools_enabled.lower() in ("0", "false", "no")`。

`mcp_server.py:181-182`：`int(os.getenv("MCP_PORT", "9381"))` → `settings.mcp_port`；`os.getenv("MCP_HOST", "127.0.0.1")` → `settings.mcp_host`。

- [ ] **Step 5: api.py lifespan 日志参数切换**

```python
    setup_logging(
        log_dir=settings.log_dir,
        console_level=settings.log_console_level,
        file_level=settings.log_file_level,
    )
```

（顶部 utils import 区加 `from lawApp_LangGraph.config import settings`；`os` 若无其他使用则清理 import。）

- [ ] **Step 6: 全量 pytest + 残留扫描**

Run: `$PY -m pytest tests/ -q`
Expected: 18 passed（Task 2 净增 1）

Run: `grep -rn "os.getenv" lawApp_LangGraph --include="*.py" | grep -v node/`
Expected: 仅剩 `RAG_program.py` 的 `HF_ENDPOINT` 一处（豁免项）

- [ ] **Step 7: Commit**

```bash
git add lawApp_LangGraph/LangGraph_lawApp.py lawApp_LangGraph/tools/rag_tools.py lawApp_LangGraph/RAG_service/embedder.py lawApp_LangGraph/RAG_service/pinecone_retriever.py lawApp_LangGraph/RAG_service/RAG_program.py lawApp_LangGraph/db.py lawApp_LangGraph/runtime.py lawApp_LangGraph/mcp_client.py lawApp_LangGraph/mcp_server.py lawApp_LangGraph/FastAPI/api.py
git commit -m "C: 外围切换 — LLM/检索/DB/MCP/日志参数全部改引settings(仅剩HF_ENDPOINT豁免)"
```

---

### Task 4: 单例并发治理 — 6 处懒加载加锁

**Files:**
- Modify: `lawApp_LangGraph/LangGraph_lawApp.py`（get_planner_llm / get_executor_llm）
- Modify: `lawApp_LangGraph/tools/rag_tools.py`（_get_llm）
- Modify: `lawApp_LangGraph/RAG_service/embedder.py`（get_embedder / get_reranker）
- Modify: `lawApp_LangGraph/RAG_service/pinecone_retriever.py`（_get_service）

**Interfaces:**
- Consumes: Task 3 后的各单例函数形态
- Produces: 各单例函数签名不变（`get_planner_llm() -> BaseChatModel` 等），仅内部加 `threading.Lock` 双检；测试 monkeypatch 点（`app.get_planner_llm` / `rag_tools._get_llm` 等）不受影响

- [ ] **Step 1: 统一加锁模式**

每个文件顶部（import 区）加 `import threading`，单例变量旁加模块级 `_xxx_lock = threading.Lock()`。模式（以 get_executor_llm 为例，其余 5 处同理）：

```python
_llm_executor = None
_llm_executor_lock = threading.Lock()


def get_executor_llm():
    """Pro LLM（规划/重规划，强推理）。测试可 monkeypatch 本函数。"""
    global _llm_executor
    if _llm_executor is None:
        with _llm_executor_lock:
            if _llm_executor is None:
                from langchain_openai import ChatOpenAI

                _llm_executor = ChatOpenAI(
                    model=settings.deepseek_flash_model,
                    temperature=0.25,
                    max_tokens=2048,
                    openai_api_key=settings.deepseek_api_key,
                    openai_api_base=settings.deepseek_base_url,
                )
    return _llm_executor
```

6 个适用点：
1. `LangGraph_lawApp.get_planner_llm`（`_llm_planner_lock`）
2. `LangGraph_lawApp.get_executor_llm`（`_llm_executor_lock`）
3. `rag_tools._get_llm`（`_llm_lock`）
4. `embedder.get_embedder`（`_embedder_lock`）
5. `embedder.get_reranker`（`_reranker_lock`；注意禁用分支 `return None` 不缓存，语义不变）
6. `pinecone_retriever._get_service`（`_service_lock`）

- [ ] **Step 2: 语法验证**

Run: `$PY -c "import lawApp_LangGraph.LangGraph_lawApp, lawApp_LangGraph.tools.rag_tools, lawApp_LangGraph.RAG_service.embedder, lawApp_LangGraph.RAG_service.pinecone_retriever; print('ok')"`
Expected: ok

- [ ] **Step 3: 全量 pytest**

Run: `$PY -m pytest tests/ -q`
Expected: 18 passed（no_llm fixture 的 monkeypatch 点是函数本身，锁不影响）

- [ ] **Step 4: Commit**

```bash
git add lawApp_LangGraph/LangGraph_lawApp.py lawApp_LangGraph/tools/rag_tools.py lawApp_LangGraph/RAG_service/embedder.py lawApp_LangGraph/RAG_service/pinecone_retriever.py
git commit -m "C: 单例并发治理 — 6处懒加载threading.Lock双检(多worker/线程演示防护)"
```

---

### Task 5: JSON 日志 — _JsonFormatter + force 参数

**Files:**
- Modify: `lawApp_LangGraph/FastAPI/logging.py`
- Test: `tests/test_engineering.py`（追加用例）

**Interfaces:**
- Consumes: 现有 `_BaseLogger` 的 extra 字段机制（`detail`/`result`/`summary`）
- Produces:
  - `_JsonFormatter`（文件 handler 专用：`agent_flow.log` 与 `system.log` 输出 JSON 行）
  - `setup_logging(log_dir=..., console_level=..., file_level=..., force: bool = False)` —— 新增 force 参数供测试重复初始化
  - JSON 行字段：`{"ts", "level", "session", "logger", "msg", "detail", "result"}`（summary 同理；无值省略该键）

- [ ] **Step 1: 写失败测试（追加到 test_engineering.py 末尾）**

```python
def test_json_logging(tmp_path):
    """文件日志为 JSON 行, 结构化字段独立成键; force 可重复初始化。"""
    import json as _json

    import lawApp_LangGraph.FastAPI.logging as lg

    lg.setup_logging(
        log_dir=str(tmp_path), console_level="ERROR",
        file_level="DEBUG", force=True,
    )
    try:
        lg.set_session("s-json-test")
        lg.flow.info("JSON日志验证", detail="known=3/7", result="elapsed=0.42s")
        for h in lg.agent_flow.handlers:
            h.flush()
        lines = (tmp_path / "agent_flow.log").read_text(
            encoding="utf-8"
        ).strip().splitlines()
        obj = _json.loads(lines[-1])
        assert obj["level"] == "INFO"
        assert obj["session"] == "s-json-test"[:8]
        assert obj["logger"] == "agent_flow"
        assert obj["msg"] == "JSON日志验证"
        assert obj["detail"] == "known=3/7"
        assert obj["result"] == "elapsed=0.42s"
    finally:
        # 清掉指向 tmp_path 的 handler, 不污染后续用例
        for logger in lg._loggers.values():
            logger.handlers.clear()
```

- [ ] **Step 2: 运行验证失败**

Run: `$PY -m pytest tests/test_engineering.py::test_json_logging -v`
Expected: FAIL（TypeError: setup_logging() got an unexpected keyword argument 'force'）

- [ ] **Step 3: 改 logging.py**

1. 顶部 import 区加 `import json`
2. `_FileFormatter` 类整体替换为：

```python
class _JsonFormatter(logging.Formatter):
    """文件 JSON 行格式化器: 结构化字段独立成键, 供日志采集消费。

    字段: ts/level/session/logger/msg + summary/detail/result(有值才有键)。
    """

    def format(self, record: logging.LogRecord) -> str:
        obj = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "session": get_session()[:8],
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for attr in ("summary", "detail", "result"):
            val = getattr(record, attr, None) or ""
            if val:
                obj[attr] = val
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        return json.dumps(obj, ensure_ascii=False)
```

3. `_FILE_FMT` 常量随 `_FileFormatter` 删除；两处 `setFormatter(_FileFormatter())`（flow_file_handler 与 sys_file_handler）改 `setFormatter(_JsonFormatter())`
4. 模块 docstring 的「日志格式」段更新为：文件 JSON 行 / 控制台彩色人类可读

- [ ] **Step 4: setup_logging 加 force**

签名与守卫改为：

```python
def setup_logging(
    log_dir: str = "./logs",
    console_level: int | str = logging.INFO,
    file_level: int | str = logging.INFO,
    force: bool = False,
) -> None:
```

```python
    global _initialized
    if _initialized and not force:
        return
```

（docstring 的 Args 段补 `force: 重复初始化(测试用), 重挂 handler 前先清理旧 handler`；函数体其余不动——循环里已有 `logger.handlers.clear()`。）

- [ ] **Step 5: 运行验证通过**

Run: `$PY -m pytest tests/test_engineering.py::test_json_logging -v`
Expected: PASS

- [ ] **Step 6: 全量 pytest**

Run: `$PY -m pytest tests/ -q`
Expected: 19 passed

- [ ] **Step 7: Commit**

```bash
git add lawApp_LangGraph/FastAPI/logging.py tests/test_engineering.py
git commit -m "C: JSON日志 — 文件handler改JsonFormatter(结构化字段独立成键)+setup_logging加force"
```

---

### Task 6: 死字段清理 + legacy node/ 删除

**Files:**
- Modify: `lawApp_LangGraph/state.py`（AgentState 删 9 字段）
- Modify: `lawApp_LangGraph/LangGraph_lawApp.py`（ingest 重置行 + `_STATE_KEYS`）
- Delete: `lawApp_LangGraph/node/`（整目录：`langgraph_nodes.py` + `nodes_test.ipynb`）
- Test: `tests/test_engineering.py`（追加用例）

**Interfaces:**
- Consumes: 无（删除类任务）
- Produces:
  - `AgentState` 无 `session_id`/`user_id`/`is_pdf_output`/`crag_context`/`memory_results`/`memory_update`/`is_law_questions`/`is_simple_questions`/`should_continue` 9 字段
  - `_STATE_KEYS` 收缩为 8 键：`rag_documents`/`evaluation`/`final_answer`/`final_prompts`/`web_search_results`/`pdf_path`/`law_results`/`prompts_record`
  - 工具返回字典（`db_tools.py`/`tools.py`）**保持不动**——`memory_results` 等键作为 ToolMessage 内容仍是执行器 LLM 上下文，仅不再合入 state
  - 保留字段（后续任务依赖）：`final_prompts`/`pdf_path`/`reasoning`/`hitl_event`/`case_elements`/`clarify_history`

- [ ] **Step 1: 写失败测试（追加到 test_engineering.py 末尾）**

```python
def test_state_dead_fields_removed():
    """9 个死字段已删; 存活字段(有消费方/B预留)仍在。"""
    from lawApp_LangGraph.state import AgentState

    dead = (
        "session_id", "user_id", "is_law_questions", "is_simple_questions",
        "should_continue", "crag_context", "memory_results", "memory_update",
        "is_pdf_output",
    )
    for f in dead:
        assert not hasattr(AgentState, f), f"死字段未删: {f}"
    for alive in (
        "final_prompts", "pdf_path", "reasoning", "hitl_event",
        "case_elements", "clarify_history", "pending_questions",
    ):
        assert hasattr(AgentState, alive), f"存活字段缺失: {alive}"
```

- [ ] **Step 2: 运行验证失败**

Run: `$PY -m pytest tests/test_engineering.py::test_state_dead_fields_removed -v`
Expected: FAIL（AssertionError: 死字段未删: session_id）

- [ ] **Step 3: 删 state.py 的 9 个字段定义**

按行号删除（现行号基于 Task 2/3 改动后可能漂移 ±10 行，以内容定位为准）：

- 309-311 行：`# 会话标识` 注释 + `session_id: str = ...` + `user_id: Optional[str] = None`
- 315-316 行：`# 是否输出为 PDF` 注释 + `is_pdf_output: bool = False`
- 376-377 行：`# 拼装后的 CRAG 上下文（覆盖）` 注释 + `crag_context: EvaluationResult = ...`
- 378-379 行：`# 长期记忆检索结果（覆盖）` 注释 + `memory_results: List[Dict[str, Any]] = ...`
- 384-385 行：`# 长期记忆写入确认（覆盖）` 注释 + `memory_update: Optional[Dict[str, Any]] = None`
- 389-390 行：`is_law_questions: bool = False` + `is_simple_questions: bool = False`
- 393-394 行：`# 流程控制` 注释 + `should_continue: bool = True`

删除后清理：`uuid` import 若仅被 session_id 使用则删除（grep `uuid` 验证）；AgentState docstring 不动。

- [ ] **Step 4: 删 LangGraph_lawApp.py 的 ingest 重置行与 _STATE_KEYS 条目**

`ingest_node` 返回 dict 删 4 行：`"crag_context": EvaluationResult(),`、`"memory_results": [],`、`"memory_update": None,`、`"is_pdf_output": False,`（其余行不动，`"pdf_path": None` 保留）。

`_STATE_KEYS` 收缩为：

```python
_STATE_KEYS = {
    "rag_documents",
    "evaluation",
    "final_answer",
    "final_prompts",
    "web_search_results",
    "pdf_path",
    "law_results",
    "prompts_record",
}
```

- [ ] **Step 5: 删除 legacy node/ 目录**

```bash
git rm -r lawApp_LangGraph/node/
```

（全仓零引用已验证：`grep -rn "langgraph_nodes\|node\.NODES" --include="*.py" lawApp_LangGraph tests scripts` 无结果。）

- [ ] **Step 6: 运行验证通过 + 全量 pytest**

Run: `$PY -m pytest tests/test_engineering.py::test_state_dead_fields_removed -v`
Expected: PASS

Run: `$PY -m pytest tests/ -q`
Expected: **20 passed**（存量 16 无死字段直接断言，已验证；若有 FAIL 逐个修断言而非回退删除）

- [ ] **Step 7: 残留扫描**

Run: `grep -rn "crag_context\|memory_results\|memory_update\|is_pdf_output\|should_continue\|is_law_questions\|is_simple_questions\|state\.session_id\|state\.user_id" lawApp_LangGraph --include="*.py" | grep -v "db_tools\|tools/tools"`
Expected: 仅剩 `db_tools.py`/`tools.py` 工具返回字典中的键（设计保留）与 `node/`（已删）之外的零星注释可忽略；任何 `state.X` 形式的读取都必须为零

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "C: 死字段清理 — AgentState删9字段+_STATE_KEYS收缩+ingest重置行清理+legacy node/整目录删除"
```

---

### Task 7: notebook 常量引用更新 + 全链路回归

**Files:**
- Modify: `lawApp_LangGraph/clarify_test.ipynb`（import 行 + 1 处 `MAX_CLARIFY_ROUNDS`）
- Modify: `lawApp_LangGraph/hitl_test.ipynb`（`app.MAX_ROUNDS` ×2）

**Interfaces:**
- Consumes: Task 2 删除的 `state.MAX_CLARIFY_ROUNDS` / `LangGraph_lawApp.MAX_ROUNDS`
- Produces: 3 个 A notebook 在新常量体系下全部可执行（`prompts_test.ipynb` 无常量引用，已验证，仅复跑）

- [ ] **Step 1: 更新 clarify_test.ipynb**

用 nbformat 程序化改（不建议手编 JSON）：

```bash
$PY - <<'EOF'
import nbformat
from pathlib import Path

p = Path("lawApp_LangGraph/clarify_test.ipynb")
nb = nbformat.read(p, as_version=4)
for cell in nb.cells:
    if cell.cell_type != "code":
        continue
    src = cell.source
    src = src.replace(
        "    CaseElements, default_case_elements, MAX_CLARIFY_ROUNDS,",
        "    CaseElements, default_case_elements,",
    )
    src = src.replace(
        "MAX_CLARIFY_ROUNDS", "settings.max_clarify_rounds"
    )
    if "settings.max_clarify_rounds" in src and "from lawApp_LangGraph.config import" not in src:
        src = src.replace(
            "from lawApp_LangGraph.state import",
            "from lawApp_LangGraph.config import settings\nfrom lawApp_LangGraph.state import",
            1,
        )
    cell.source = src
nbformat.write(nb, p)
print("clarify_test.ipynb updated")
EOF
```

（脚本逻辑：先缩 import 行去掉常量名，再把剩余 `MAX_CLARIFY_ROUNDS` 替换为 `settings.max_clarify_rounds` 并补 settings import；执行后需人工打开 diff 确认三处改动落位。若 replace 未命中任何单元格，说明 notebook 源与本计划记载有出入——停下按实际源码调整，不要盲目执行。）

- [ ] **Step 2: 更新 hitl_test.ipynb**

```bash
$PY - <<'EOF'
import nbformat
from pathlib import Path

p = Path("lawApp_LangGraph/hitl_test.ipynb")
nb = nbformat.read(p, as_version=4)
for cell in nb.cells:
    if cell.cell_type != "code":
        continue
    src = cell.source
    if "app.MAX_ROUNDS" in src or "MAX_ROUNDS" in src:
        src = src.replace("app.MAX_ROUNDS", "settings.max_rounds")
        if "from lawApp_LangGraph.config import settings" not in src:
            src = (
                "from lawApp_LangGraph.config import settings\n" + src
            )
        cell.source = src
nbformat.write(nb, p)
print("hitl_test.ipynb updated")
EOF
```

- [ ] **Step 3: 三个 notebook 回归**

```bash
$PY scripts/run_nb.py lawApp_LangGraph/clarify_test.ipynb
$PY scripts/run_nb.py lawApp_LangGraph/hitl_test.ipynb
$PY scripts/run_nb.py lawApp_LangGraph/prompts_test.ipynb
```

Expected: 三行 `OK: xxx.ipynb all cells executed`（无异常、末尾 ALL PASSED 由 notebook 自身保证）

- [ ] **Step 4: Commit**

```bash
git add lawApp_LangGraph/clarify_test.ipynb lawApp_LangGraph/hitl_test.ipynb
git commit -m "C: notebook同步 — 澄清/中断测试改引settings + 3个notebook回归通过"
```

---

### Task 8: 文档同步 — PROJECT_OVERVIEW / README

**Files:**
- Modify: `lawApp_LangGraph/PROJECT_OVERVIEW.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: Task 1-7 全部落地结果
- Produces: 文档与代码一致（消除 §12 滞后、目录结构、配置一览表）

- [ ] **Step 1: 重写 PROJECT_OVERVIEW §12 已知问题**

替换「## 十二、已知问题与改进方向」整节为（5 条现状全部更新）：

```markdown
## 十二、已知问题与改进方向

1. **角色切换粒度**：分析角色支持环境变量切换（`LEGAL_ANALYSIS_ROLE=saul` 彩蛋，默认 Kim Wexler 全链路统一）；会话级/用户偏好级动态切换留待后续迭代
2. **评估阈值调优**：三档阈值已配置化（`CORRECT_THRESHOLD=0.5` / `INCORRECT_THRESHOLD=0.2` / `MIN_QUALITY_DOCS=3`，env 可覆盖），具体取值仍需根据检索质量持续调优
3. **并发性能**：懒加载单例已加 `threading.Lock` 双检防护；多 worker 生产部署仍建议连接池（作品集定位暂不做）
4. **递归限制**：已配置化（`RECURSION_LIMIT` 默认 60，env 可覆盖）；极复杂查询若触发 `GRAPH_RECURSION_LIMIT` 可调大
5. ~~**BM25 路径硬编码**~~：已修复（`RAG_program.py` 模块相对路径 + `BM25_PATH` env 覆盖）
```

- [ ] **Step 2: 更新 §13 目录结构**

`lawApp_LangGraph/` 树中：删除 `node/` 备用节点工厂两行（`├── node/` 及其子项）；在 `state.py` 行后加一行：

```
├── config.py                   # 运行时配置中心 (pydantic-settings, env 同名覆盖)
```

tests 区（若有目录树提及）同步 `test_engineering.py`。

- [ ] **Step 3: 新增配置一览表**

在「## 十四、部署与运行」之后新增一节：

```markdown
## 十五、配置一览（lawApp_LangGraph/config.py）

优先级：进程 env > `.env` 文件 > 默认值。完整字段见 `config.py`，常用项：

| env 名 | 默认值 | 说明 |
|--------|--------|------|
| RECURSION_LIMIT | 60 | LangGraph 超步上限 |
| MAX_ROUNDS | 10 | 工具调用总数上限 |
| MAX_CLARIFY_ROUNDS | 5 | 入口澄清轮数上限 |
| ERROR_STREAK_THRESHOLD | 2 | 连续失败触发降级询问 |
| CORRECT_THRESHOLD / INCORRECT_THRESHOLD / MIN_QUALITY_DOCS | 0.5 / 0.2 / 3 | CRAG 评估三档 |
| DEEPSEEK_PRO_MODEL / DEEPSEEK_FLASH_MODEL | deepseek-reasoner / deepseek-chat | Pro/Flash 模型 |
| MEMORY_EMBED_MODEL / RERANK_MODEL / EMBED_DIM | BAAI/bge-large-zh-v1.5 / BAAI/bge-reranker-large / 1024 | 嵌入/重排序 |
| RERANK_ENABLED | 1 | "0" 禁用重排序 |
| BM25_PATH | (模块路径兜底) | BM25 参数文件 |
| PINECONE_INDEX_NAME / PINECONE_API_KEY | pinecone-test-lawapp / - | Pinecone 检索 |
| LOG_DIR / LOG_CONSOLE_LEVEL / LOG_FILE_LEVEL | ./logs / DEBUG / INFO | 日志 |
| MCP_SERVER_URL / MCP_TOOLS_ENABLED | http://127.0.0.1:9381/mcp / 1 | MCP 客户端 |
| DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_PASSWORD / DB_POOL_MAX | localhost / 5432 / Law_app / postgres / postgres / 10 | PostgreSQL |
| DATABASE_URL / CHECKPOINT_BACKEND | - / auto | 连接串直连 / 持久化后端选择 |
```

（文末「文档生成时间 / 项目版本」更新为 `2026-09-11 | v3.2.0 (子项目C)`。）

- [ ] **Step 4: README 核对**

`README.md` 逐项核对并同步（grep 定位）：
- 工程亮点表：补「配置中心（pydantic-settings 单一入口）」「JSON 文件日志（可观测性）」两行；HITL/测试相关行核对用例数（pytest 16→21，ipynb 16 用例不变）
- 环境变量/部署段：与 §15 配置表一致（`PINECONE_INDEX_NAME=pinecone-law-agent` 若与 config 默认 `pinecone-test-lawapp` 不一致，保留 README 示例值并在该行注明"示例值，默认见 config.py"）
- 目录结构段（若有）：删 node/、加 config.py
- 已知问题段（若有 §12 类内容）：同步 Step 1 的 5 条

- [ ] **Step 5: 终验**

```bash
$PY -m pytest tests/ -q                    # 20 passed
$PY scripts/run_nb.py lawApp_LangGraph/clarify_test.ipynb    # OK
$PY scripts/run_nb.py lawApp_LangGraph/hitl_test.ipynb       # OK
$PY scripts/run_nb.py lawApp_LangGraph/prompts_test.ipynb    # OK
git status --short                          # 干净
```

- [ ] **Step 6: Commit**

```bash
git add lawApp_LangGraph/PROJECT_OVERVIEW.md README.md
git commit -m "C: 文档同步 — §12已知问题更新/配置一览表/目录删node加config/版本v3.2.0"
```

---

## 验收清单（全部任务完成后）

- [ ] `pytest tests/ -q` → 20 passed（16 存量 + 4 工程：test_config_defaults_and_env / test_graph_config_recursion / test_json_logging / test_state_dead_fields_removed）
- [ ] 3 个 notebook 复跑 OK
- [ ] `os.getenv` 全仓仅剩 `HF_ENDPOINT` 豁免项
- [ ] `git log --oneline` 含 8 个 `C:` 前缀提交
- [ ] `git status` 干净
