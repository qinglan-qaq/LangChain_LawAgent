# 子项目B「Agent as MCP Server」实现方案 — 把法律咨询 Agent 整体对外暴露为 MCP server

**Goal:** 现有 MCP 能力只有「检索原语」(law-search: `search_laws` / `search_cases` / `recall_memory`)，任何 MCP client 拿不到 Agent 的完整能力(要素澄清、规划-执行、CRAG 评估、重规划、六处 HITL、长期记忆写入)。本方案新增第二个 MCP server `law-consult`，把 **整图 Agent** 作为 MCP 工具对外暴露，使 Cursor / Claude Desktop / 其他 Agent 一次工具调用即得到完整法律咨询答复。

**Architecture:** 新增 `lawApp_LangGraph/mcp/mcp_agent_server.py`，与既有 `lawApp_LangGraph/mcp/mcp_server.py`(law-search, 9381) 并列，独立端口 9382。server 生命周期内装配 `runtime.setup_runtime()` 得到带 checkpointer + store 的图(因此图内记忆工具 `get_store()` 可用)。HITL 六处 interrupt 通过 **服务端默认策略(auto)自动应答** 收敛为单次工具调用；需要人机交互的 client 可传 `interactive=true` 走「暂停 → 返回问题 → `consult_resume` 续跑」两段式。HITL 类型归一完全复用 `FastAPI/utils.py` 的 `extract_interrupt` / `normalize_resume` / `build_response`，不重复实现。

**Tech Stack:** Python 3.11 / LangGraph 1.0.1 / mcp 1.30.0(内置 FastMCP) / langchain-mcp-adapters 0.3.2 / pytest

## Global Constraints

- 解释器一律用 `F:/Anaconda_env/lawApp_langGraph/python.exe`(下称 `$PY`)；测试与手工验证均用此解释器
- **不用 `fastmcp` 第三方包**：mcp 1.30.0 自带的 `mcp.server.fastmcp.FastMCP`，与 `requirements.txt:16` 的既有约束一致(fastmcp 2.x/4.x 与本栈的 adapters 存在 mcp 主次版本冲突)
- 两个 MCP server 职责严格分离，不复用同一个 FastMCP 实例：
  - `law-search`(9381, `lawApp_LangGraph/mcp/mcp_server.py`)：无 LLM、无状态、零成本检索原语，保持现状不动
  - `law-consult`(9382, `lawApp_LangGraph/mcp/mcp_agent_server.py`)：有状态、走 LLM、耗时耗配额的全图 Agent
- 六个 interrupt 类型标签字符串精确匹配(前端/API 依赖)：`risk_confirm` / `clarify` / `pdf_confirm` / `degrade_confirm` / `mid_clarify` / `budget_confirm`
- 默认绑定 `127.0.0.1`；绑定非回环地址时必须显式配置 Bearer token，否则拒绝启动(见 Task 6)
- 所有新增/改写函数 docstring 用 Google 风格(与子项目A 计划约束一致)；节点/工具内不画 `# ----` 分隔线
- 每个任务结束 `git commit`，message 中文，前缀 `B:`，格式仿照 `A2: MCP 双向 — ...`
- 新测试不联网、不调真实 LLM：复用 `tests/test_smoke.py` 的 `_FakeLLM` / `no_llm` 替身模式

## 设计决策(为什么这么做)

### D1. 为什么不是把 law-consult 加进现有 `lawApp_LangGraph/mcp/mcp_server.py`
现有 server 刻意「工具不依赖图执行上下文」(见 `lawApp_LangGraph/mcp/mcp_server.py:12-15`)，为此自己造了一个 store 实例。Agent 需要的是**带 checkpointer 的编译图**，两者生命周期与失败模式完全不同(检索 server 可无 DB 常驻，Agent server 依赖 DB/API Key/嵌入模型)。混在一个进程里，检索原语会被 Agent 的依赖拖下水。

### D2. HITL 怎么过 MCP
MCP 工具是请求-响应语义，而图有 6 处 `interrupt()`。三条路径，本方案 P0 实现前两条：

| interrupt type | auto 策略值 | 理由 |
|---|---|---|
| `risk_confirm` | `True`(继续) | 高风险提示文本本身随答复返回，用户仍看得到热线信息 |
| `clarify` | `""`(跳过) | 无人可问 → 按原问题继续(与前端「跳过」同语义) |
| `mid_clarify` | `""`(跳过) | 直接走联网兜底 |
| `pdf_confirm` | `False`(跳过) | PDF 落地是本地交付关注点，MCP client 拿不到文件 |
| `degrade_confirm` | `"skip"` | 继续剩余步骤 |
| `budget_confirm` | `"finish"` | 用现有材料收尾 |

1. **auto 模式(P0，默认)**：`interactive=false`。每轮 `ainvoke` 后读 `graph.aget_state(config).interrupts`，命中即用上表的值 `ainvoke(Command(resume=value))` 续跑，直到无 interrupt 或超过 `mcp_agent_max_resumes`(默认 8)。auto 路径**跳过 `normalize_resume`**——上表的值本身就是归一后的类型(bool / "skip" / "finish" / 空串)。
2. **两段式(P0)**：`interactive=true`。首个 interrupt 立即返回 `status: awaiting_input` + `session_id` + 问题载荷(元素面板、选项)，client 调 `consult_resume(session_id, answer)`；resume 值经 `normalize_resume(itype, answer)` 归一，保证与 FastAPI 侧行为一致。
3. **MCP elicitation(P2，可选)**：mcp 1.30 支持 `ctx.elicit()`，且 `ClientCapabilities.elicitation` 可探测；但 client 支持面不齐(Cursor 等未必实现)。检出能力时用 elicit 就地问，否则回落 auto。放 P2 不做进 P0。

### D3. 会话语义
MCP 调用无状态，故会话靠 `session_id` + checkpointer 显式传递。`session_id` 不传 => 服务端生成新 uuid 并**在返回文本首行回显**，client 后续带上即可多轮续聊。不需要服务端内存注册表：interrupt 载荷可由 `graph.aget_state` 重算；Postgres 后端下进程重启后 `consult_resume` 仍可用(InMemory 后端只在进程生命期内有效，需在文档中说明)。同 `session_id` 并发调用用模块级 `asyncio.Lock` 串行化，防 checkpointer 写冲突。

### D4. 超时与成本
单次 consult 走 Pro 规划 + Flash 执行，实测数十秒到分钟级，MCP client 常有调用超时。三件事：①工具 docstring 明确写出「耗时数十秒、消耗 LLM 配额」，让调用方 LLM 有预期；②`asyncio.wait_for` 包住整个 resume 循环，`mcp_agent_budget_seconds`(默认 600) 到点返回「部分结果 + session_id」，client 可 `get_consultation` 或再次 resume 拿最终答复；③`ctx.report_progress` + `ctx.log` 报告节点级进度，支持进度的 client 不会静默挂死。

### D5. stdio 传输的 stdout 洁净性(P0 必做)
stdio 传输下 stdout 只允许 JSON-RPC 帧。`FastAPI/logging.py:173` 的 `setup_logging()` 把 `StreamHandler(sys.stdout)` 挂在所有 logger 上；Agent 节点内大量 `debug.*` 调用一旦经此路径输出到 stdout，协议帧即被污染、client 解析失败。故 `setup_logging` 增加 `console_stream` 参数(默认 `sys.stdout` 不变，向后兼容)，stdio 入口显式传 `sys.stderr`，且该入口**不调用** `logging.basicConfig`。

## File Structure(全局地图)

```
lawApp_LangGraph/
├── mcp/                        # MCP 子包(2026-09-18 由顶层迁入)
│   ├── mcp_server.py           # 既有 — law-search server(9381), 保持现状不动
│   ├── mcp_client.py           # Task 4: 自挂载守卫
│   ├── mcp_agent_server.py     # Task 2: 新建 — law-consult server(3 工具 + HITL 策略)
│   └── mcp_agent_stdio.py      # Task 3: 新建 — stdio 入口(区别于测试生成的 mcp_server_stdio.py)
├── config.py                   # Task 1: 新增 mcp_agent_* 配置
└── FastAPI/
    └── logging.py              # Task 3: setup_logging(console_stream=...)
tests/
├── conftest.py                 # Task 5: 上移 no_llm / _FakeLLM 替身
└── test_mcp_agent.py           # Task 5: 新建
README.md / docs/PROJECT_OVERVIEW.md / .cursor/mcp.json   # Task 6: 文档与挂载配置
```

依赖顺序：Task 1 → 2 → 3 → 4 → 5 → 6。Task 2 是本方案主体。

---

### Task 1: 配置项

**Files:**
- Modify: `lawApp_LangGraph/config.py`(在 `# ============ MCP ============` 段尾追加)

**Interfaces:**
- Produces: `settings.mcp_agent_port` / `mcp_agent_host` / `mcp_agent_transport` / `mcp_agent_interactive` / `mcp_agent_max_resumes` / `mcp_agent_budget_seconds` / `mcp_agent_token` / `mcp_agent_enabled`，Task 2/3/6 全部引用这些名字

- [ ] **Step 1: 追加字段**(字符串开关保持既有 "0"/"false"/"no" 语义，见 `config.py:79-80` 注释风格)

```python
    # Agent-as-MCP-server(law-consult)。与 law-search(9381) 分开: 本服务走 LLM、有状态、耗配额
    mcp_agent_enabled: str = "1"
    mcp_agent_host: str = "127.0.0.1"
    mcp_agent_port: int = 9382
    mcp_agent_transport: str = "streamable-http"  # streamable-http | stdio
    # 默认 auto: 服务端按策略自动应答 HITL,单次工具调用收敛; "1" 则暂停等 consult_resume
    mcp_agent_interactive: str = "0"
    mcp_agent_max_resumes: int = 8
    mcp_agent_budget_seconds: int = 600
    # 非回环绑定时的 Bearer token; 回环地址可空
    mcp_agent_token: Optional[str] = None
```

- [ ] **Step 2: 验证**

```bash
$PY -c "from lawApp_LangGraph.config import Settings; s=Settings(_env_file=None); print(s.mcp_agent_port, s.mcp_agent_max_resumes)"
```

- [ ] **Step 3: commit** — `B: 配置 — 新增 mcp_agent_* 系列(端口/传输/HITL 策略/预算/token)`

---

### Task 2: law-consult server 主体

**Files:**
- Create: `lawApp_LangGraph/mcp/mcp_agent_server.py`

**Interfaces:**
- Produces:
  - `mcp`(`FastMCP("law-consult")` 实例，Task 3 的 stdio 入口 `from ...mcp_agent_server import mcp`)
  - `consult_law(query: str, session_id: str = "", interactive: bool = False) -> str`
  - `consult_resume(session_id: str, answer: str) -> str`
  - `get_consultation(session_id: str) -> str`
  - `AUTO_RESUME: dict[str, object]`(D2 策略表，Task 5 单测直接断言此表)
- Consumes: `runtime.setup_runtime` / `teardown_runtime`、`FastAPI/utils.py` 的 `graph_config` / `extract_interrupt` / `normalize_resume` / `build_response`、`db.close_pool`

- [ ] **Step 1: lifespan 装配图**

FastMCP 1.30 的 `FastMCP.__init__(lifespan=...)` 接受一个 `async contextmanager`，`mcp.run()` / `streamable_http_app()` 都会跑它：

```python
@asynccontextmanager
async def _lifespan(server: FastMCP):
    """服务生命周期内装配一次图(checkpointer + store),并把图内记忆工具的 get_store() 打通。"""
    from lawApp_LangGraph import runtime
    await runtime.setup_runtime()
    try:
        yield {"graph": runtime.graph, "backend": runtime.checkpoint_backend}
    finally:
        await runtime.teardown_runtime()
        from lawApp_LangGraph.db import close_pool
        await close_pool()


mcp = FastMCP("law-consult", lifespan=_lifespan)
```

注意 log 走 stderr（不用 `logging.basicConfig` 到 stdout）。

- [ ] **Step 2: auto 策略 + resume 循环**

```python
AUTO_RESUME: dict[str, object] = {
    "risk_confirm": True,      # 继续; 热线提示随答复返回
    "clarify": "",             # 跳过要素反问, 按原问题继续
    "mid_clarify": "",         # 跳过, 转联网兜底
    "pdf_confirm": False,      # 跳过 PDF 落地
    "degrade_confirm": "skip",  # 继续剩余步骤
    "budget_confirm": "finish",  # 现有材料收尾
}


async def _drive(graph, config, payload, interactive: bool, ctx) -> tuple[dict, Optional[dict]]:
    """推进图至终态或无 interrupt; 返回 (终态 state 或 {} , 待人工回复的 interrupt 载荷或 None)。

    auto: 命中 interrupt 即用 AUTO_RESUME 的值 Command(resume=...) 续跑, 最多 mcp_agent_max_resumes 轮;
    interactive: 首次 interrupt 立即返回, 由 consult_resume 接手。
    """
```

要点：
- 每轮用 `await graph.aget_state(config)` + `extract_interrupt()` 判断是否暂停，命中且 `interactive=False` 则 `AUTO_RESUME[itype]` 续跑；`itype` 不在表中(未来新增类型) → 记 warning 并按 `""` 放行，避免死循环。
- 循环整体包 `asyncio.wait_for(..., settings.mcp_agent_budget_seconds)`；超时返回已收集的 state + `timed_out` 标记。
- auto 模式收集 `auto_resolved: list[str]`(被自动应答的 interrupt 类型)，写进答复末尾的「本次自动处理」段落，让用户知道哪些澄清被跳过了。
- 每轮 `await ctx.report_progress(...)` 与 `ctx.log("info", ...)` 报告当前节点/步骤。

- [ ] **Step 3: 三个 `@mcp.tool()`**

```python
@mcp.tool()
async def consult_law(query: str, session_id: str = "", interactive: bool = False, ctx: Context = None) -> str:
    """完整法律咨询(注意: 单次调用通常耗时 30-180 秒, 会消耗 LLM 配额)。

    执行要素澄清 → 检索法条/案例 → 质量评估 → (必要时)重规划 → 生成答复 的全流程。
    高风险问题会返回安全提示与热线信息。

    Args:
        query: 用户的法律问题, 中文, 越具体越好(时间/金额/是否起诉等要素)
        session_id: 会话 id; 留空则新建, 新 id 在答复首行回显, 后续追问请带上同一 id
        interactive: True=遇到需要确认/补充时暂停并返回问题(再用 consult_resume 回复);
            False(默认)=服务端按默认策略自动处理, 一次调用返回最终答复
    """
```

```python
@mcp.tool()
async def consult_resume(session_id: str, answer: str) -> str:
    """回复咨询中的人工确认/补充问题, 从暂停点继续。

    Args:
        session_id: consult_law 返回的会话 id
        answer: 自由文本回复; 是/否类问题回 "是"/"否", 选择类回选项文本, 补充类直接写内容
    """
```

```python
@mcp.tool()
async def get_consultation(session_id: str) -> str:
    """查询会话当前状态与已有结果(等待回复时返回待回答问题, 完成后返回答复)。

    Args:
        session_id: 会话 id
    """
```

三条工具统一：入口 `ensure_session`(复用 `FastAPI/utils.ensure_session`)、`set_session(sid)`(日志打点)、同 `session_id` 的 `asyncio.Lock` 串行、`graph_config(sid)` 取 config、结果统一经 `build_response(state, sid)` → markdown 文本(答复 / 引用来源 / 工具调用 / 案件要素面板 / 状态行)。实现细节照 `lawApp_LangGraph/mcp/mcp_server.py:99-111` 的纯文本拼装风格，不返回裸 dict。

- [ ] **Step 4: `main()`**

```python
def main() -> None:
    """启动 law-consult MCP server。transport 由 settings.mcp_agent_transport 决定。"""
```

streamable-http 分支设 `mcp.settings.host/port` 后 `mcp.run(transport="streamable-http")`(与 `lawApp_LangGraph/mcp/mcp_server.py:179-188` 同形)；stdio 分支 `mcp.run(transport="stdio")`。

- [ ] **Step 5: 冒烟(无 DB / 无 API Key 也不得崩)**

```bash
$PY -c "
import asyncio
from lawApp_LangGraph.mcp.mcp_agent_server import mcp
print(sorted(t.name for t in mcp._tool_manager.list_tools()))
"
```

- [ ] **Step 6: commit** — `B: MCP Server — law-consult 把整图 Agent 暴露为 MCP 工具(3 工具 + HITL auto 策略)`

---

### Task 3: stdio 入口 + stdout 洁净性

**Files:**
- Create: `lawApp_LangGraph/mcp/mcp_agent_stdio.py`(注意：**不是** `lawApp_LangGraph/mcp/mcp_server_stdio.py`，后者被 `.gitignore:30` 忽略，是 `tests/test_mcp.py` 的运行期产物)
- Modify: `lawApp_LangGraph/FastAPI/logging.py`(`setup_logging` 增参)

**Interfaces:**
- Produces: `setup_logging(..., console_stream=sys.stdout)`；`python -m lawApp_LangGraph.mcp.mcp_agent_stdio` 可被 stdio MCP client 挂载
- Consumes: Task 2 的 `mcp`

- [ ] **Step 1: `setup_logging` 加 `console_stream` 参数**(默认 `sys.stdout`，既有调用点 `api.py:57-61` 不受影响)

`fastapi/logging.py` 内两处 `StreamHandler(sys.stdout)`(173、203 行)改用该参数。

- [ ] **Step 2: 写 stdio 入口**

```python
"""law-consult 的 stdio 传输入口(MCP client 以子进程方式挂载时用)。

stdio 下 stdout 是 JSON-RPC 专用通道: 本模块不写任何日志到 stdout,
控制台日志显式导到 stderr(见 setup_logging(console_stream=...))。
"""

from lawApp_LangGraph.FastAPI.logging import setup_logging
from lawApp_LangGraph.config import settings
from lawApp_LangGraph.mcp.mcp_agent_server import mcp

if __name__ == "__main__":
    import sys

    setup_logging(
        log_dir=settings.log_dir,
        console_level=settings.log_console_level,
        file_level=settings.log_file_level,
        console_stream=sys.stderr,
        force=True,
    )
    mcp.run(transport="stdio")
```

- [ ] **Step 3: 洁净性验证**——stdio 子进程跑一轮 `initialize` + `tools/list`，断言 stdout 每行都是合法 JSON-RPC(无日志混入)；测试落在 Task 5。

- [ ] **Step 4: commit** — `B: MCP stdio — law-consult stdio 入口 + 日志控制台流可切 stderr(防污染 JSON-RPC 帧)`

---

### Task 4: 自挂载守卫

**Files:**
- Modify: `lawApp_LangGraph/mcp/mcp_client.py`

**Interfaces:**
- Consumes: `settings.mcp_agent_host` / `mcp_agent_port` / `mcp_server_url`

- [ ] **Step 1: 守卫逻辑**——`get_mcp_tools()` 建连前解析 `settings.mcp_server_url` 的 host/port，若与 `settings.mcp_agent_host:mcp_agent_port` 相同，记 warning 并跳过挂载。理由：若 Agent server 自己也挂 law-consult，`consult_law` 会成为图内工具 → 递归自调用，且每条计划步骤都可能再触发一次整图运行(LLM 成本爆炸)。
- [ ] **Step 2: 验证**：`MCP_SERVER_URL=http://127.0.0.1:9382/mcp $PY -c "import asyncio; from lawApp_LangGraph.mcp.mcp_client import get_mcp_tools; print(asyncio.run(get_mcp_tools()))"` → `[]` 且日志含跳过原因。
- [ ] **Step 3: commit** — `B: MCP 客户端 — 拒绝自挂载 law-consult(防图内递归自调用)`

---

### Task 5: 测试

**Files:**
- Create: `tests/conftest.py`、`tests/test_mcp_agent.py`
- Modify: `tests/test_smoke.py`(把 `_FakeMsg/_FakeVerdict/_FakeChain/_FakeLLM` 与 `no_llm` fixture 上移到 conftest，保持 test_smoke 语义不变)

**Interfaces:**
- Consumes: Task 2 的 `mcp` / `AUTO_RESUME` / 三个工具、Task 3 的 stdio 入口、Task 4 的守卫

- [ ] **Step 1: 工具注册**——`{"consult_law","consult_resume","get_consultation"} == {t.name ...}`
- [ ] **Step 2: auto 策略表全覆盖**——对 6 个 interrupt type 断言 `AUTO_RESUME[t]` 具体值(表本身就是契约)；unknown type 走 `""` 放行分支。
- [ ] **Step 3: 端到端 auto(no_llm，无 DB)**——图跑通一次 `consult_law`，断言：返回文本非空、含「实时性/热线」或降级文案、无 `Traceback`、返回首行含新 `session_id`。
- [ ] **Step 4: interactive 两段式**——`interactive=true` 首次调用返回 `awaiting_input` + `session_id`，`consult_resume(sid, "是")` 继续;断言 pause/resume 走通(`Command` 路径)。
- [ ] **Step 5: 余额/降级**——DB/嵌入不可用时 `consult_law` 返回「暂不可用」类文案而非异常堆栈(参照 `tests/test_mcp.py:78-127` 的降级断言风格)。
- [ ] **Step 6: stdio 洁净性(e2e)**——按 `tests/test_mcp.py:_stdio_config()` 的路子拉子进程(必须显式传 `HF_HOME` + `HF_HUB_OFFLINE=1`，否则 BGE 模型找不到会转网络重试、测试长时间挂起——该坑见 `tests/test_mcp.py:47-61` 与 commit `1c71708`)，逐个读子进程 stdout 行并 `json.loads`，断言全部可解析。
- [ ] **Step 7: 自挂载守卫**——monkeypatch `mcp_server_url` 指向 9382，断言 `get_mcp_tools() == []` 且未建连接。
- [ ] **Step 8: 全量回归** `$PY -m pytest tests/ -q`；commit — `B: 测试 — law-consult 工具注册/auto 策略/interactive 两段式/stdio 洁净性/自挂载守卫`

---

### Task 6: 文档与挂载配置

**Files:**
- Modify: `README.md`(MCP 章节与目录树：两个 server)、`docs/PROJECT_OVERVIEW.md`(§MCP、工具清单、env 表)、`.cursor/mcp.json`、`langgraph.json`(若需)

- [ ] **Step 1: `.cursor/mcp.json` 增条目**(默认保留 law-search；law-consult 需本地起服务后再启用)

```json
{
  "mcpServers": {
    "law-search": { "url": "http://127.0.0.1:9381/mcp" },
    "law-consult": { "url": "http://127.0.0.1:9382/mcp" }
  }
}
```

- [ ] **Step 2: 文档三件事**：①两 server 分工表(端口/成本/状态/依赖)；②`session_id` 多轮与 `consult_resume` 用法示例(含 `interactive` 语义)；③安全与成本边界——默认回环、非回环需 token、一次 consult 的 LLM 成本、记忆工具会**写入** store(任何已连接 client 都可写)。
- [ ] **Step 3: commit** — `B: 文档 — law-consult 挂载方式/会话语义/安全与成本边界同步`

---

## P2(不入 P0，需要时再开)

- **elicitation 就地交互**：`ctx.elicit()` 由 client 弹窗提问，检出 `ClientCapabilities.elicitation` 才用，否则回落 auto(见 D2.3)。
- **`depth="quick"` 快答档**：跳过 planner/澄清，`fetch_laws` + `retrieve_legal_knowledge` 后直接走 `FINALIZE_CASE_PROMPT` 出答复(约 15 行，成本从 Pro+Flash 多次降到 Flash 一次)。
- **MCP resource / prompt**：`law://consultation/{session_id}` 资源暴露答复，`legal_consult` prompt 模板便于 client 一键起咨询。
- **非回环绑定的 Bearer 中间件**：`mcp.streamable_http_app()` 外层包 ASGI 中间件校验 `Authorization: Bearer $MCP_AGENT_TOKEN`，用 uvicorn 自托管(注意 `session_manager` 只能在 `streamable_http_app()` 之后访问，需照抄 FastMCP 的 lifespan)。SDK 亦提供 `TokenVerifier` / `AuthSettings`，但配套 OAuth 发现路由对本地单用户场景过重。

## 风险与对策

| 风险 | 对策 |
|---|---|
| MCP client 调用超时(Agent 分钟级) | `mcp_agent_budget_seconds` + 返回部分结果与 `session_id`；docstring 明示耗时；`ctx.report_progress` 报进度 |
| auto 策略掩盖了本该问用户的关键要素 | 答复末尾固定「本次自动处理」段列出被跳过的 interrupt 类型；需要交互的 client 用 `interactive=true` |
| stdio 下 stdout 被日志污染 | Task 3 统一：`console_stream=sys.stderr` + stdio 入口不 `basicConfig`；Task 5 Step 6 用逐行 `json.loads` 守住 |
| 服务端与图内记忆工具写同一 store 冲突 | 二者共用同一 Postgres store(与 `lawApp_LangGraph/mcp/mcp_server.py:37-45` 同后端、同命名空间 `("law_agent","memories")`)；写路径经 checkpointer 串行 + `session_id` 级锁 |
| 一次调用烧掉大量 LLM 配额 | 默认 `max_rounds=10` / `max_clarify_rounds=5` 不变；`mcp_agent_max_resumes=8` 封顶 auto 续跑；文档明示成本；P2 的 quick 档作低成本替代 |

## 验证清单(整案完成标准)

1. `$PY -m pytest tests/ -q` 全绿(含存量 `tests/test_mcp.py` 不回归)
2. `$PY -m lawApp_LangGraph.mcp.mcp_agent_server` 起 9382，`tools/list` 返回 3 工具
3. Cursor(或 adapters client)挂载后问一个具体婚姻家事问题 → 单次 `consult_law` 拿到完整答复
4. `interactive=true` 走到 `clarify` 暂停 → `consult_resume` 续跑至终态
5. stdio 入口挂载可用，stdout 无日志混入
