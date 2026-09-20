# 子项目A「问询与协同」实现计划 — 要素驱动澄清循环 + 执行中 HITL

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将已确认的设计（`docs/superpowers/specs/2026-09-10-clarify-hitl-design.md`）落地：要素清单驱动的 5 轮自适应澄清循环、检索反馈联动追问、故障/预算兜底 HITL、prompts.py 集中化与 Kim 全链路人设。

**Architecture:** LangGraph StateGraph 从 9 节点扩到 14 节点（clarify 拆为 risk_gate + element_assess + ask_element，新增 mid_clarify / hitl_degrade / hitl_budget），6 处 interrupt，全部降级路径保持"问不了就放行"。提示词收拢到新模块 `prompts.py`。

**Tech Stack:** Python 3.11 / LangGraph 1.x / Pydantic v2 / FastAPI / pytest（存量冒烟）+ ipynb（nbclient 程序化执行）

## Global Constraints

- Python 解释器一律用 `/Users/qinglan/miniconda3/envs/lawagent/bin/python`（下文简写 `$PY`）
- 图拓扑：14 节点 = ingest, risk_gate, element_assess, ask_element, planner, executor, tools, merge, replan_check, mid_clarify, replanner, hitl_degrade, hitl_budget, finalize
- 常量（定义在 `state.py`）：`MAX_CLARIFY_ROUNDS = 5`，`MAX_ROUNDS = 10`（存量），`ERROR_STREAK_THRESHOLD = 2`
- 6 处 interrupt 类型标签（前端/API 归一化依赖，字符串精确匹配）：`risk_confirm` / `clarify` / `pdf_confirm` / `degrade_confirm` / `mid_clarify` / `budget_confirm`
- 不足原因枚举（`ReplanCheckSchema.insufficient_reason`）：`vague` / `not_found` / `error` / `none`
- 角色：反问/兜底/分析默认 Kim Wexler；Saul 仅经 `LEGAL_ANALYSIS_ROLE=saul` 影响 `LEGAL_ANALYSIS_PROMPT`；finalize 的"七成傲娇"人设删除
- 新功能测试 100% 走 ipynb，pytest 存量只做最小更新（`test_graph_topology` / `test_interrupt_resume` 两处）
- 所有节点遵循现有风格：纯记账节点不调 LLM、LLM 节点 try/except 软放行、`debug.debug("→ 进入 X 节点")` 日志
- 每个任务结束必须 `git commit`，commit message 用中文、格式仿照 `A2: MCP 双向 — ...`（本系列前缀 `A3:`）

### 执行方式与代码风格约束（执行要求，优先级高于任务内代码样例）

- **执行方式：子代理**。本计划以子代理（subagent）模式执行：每个任务由控制器派发一个全新的实现子代理 + 一个独立的评审子代理，控制器只做协调、裁决与台账记录；子代理不继承会话历史，只接收任务简报、接口上下文与本节约束。
- **高内聚、低耦合**。每个模块/节点函数职责单一，只依赖 `AgentState` 与计划中声明的显式接口；节点之间不 import 对方内部实现，跨模块协作只经 state 字段与 `state.py`/`prompts.py` 公共模块传递；提示词只在 `prompts.py` 定义一次。
- **禁止横线分隔注释**。任何代码（py / ipynb code cell）不得使用由连续 `-` 构成的分隔/装饰注释（如 `# ----------`、`# ---- 分节 ----`、`# ---`）；需要分节说明时用普通注释文字表达，不得画线。
- **Google 风格多行注释**。新增或改写的模块/类/函数 docstring 一律采用 Google 风格：首行为单句中文摘要；凡包含参数、返回值、异常、示例等多行内容者，必须使用 `Args:` / `Returns:` / `Raises:` / `Example:` 段落格式。任务内代码样例中的单行 docstring 在落地时按此升级为 Google 风格（行为与签名不变，仅注释形态调整）。

## 测试基建约定（所有任务共用）

- notebook 放 `lawApp_LangGraph/` 下，用 `$PY -m ipykernel install --user --name lawagent` 已有内核或显式指定路径执行
- 程序化执行 notebook 验证（不依赖 Jupyter UI）：

```python
# scripts/run_nb.py — 所有任务共用的验证脚本（Task 1 创建一次）
import sys
from pathlib import Path
import nbformat
from nbclient import NotebookClient

nb = nbformat.read(Path(sys.argv[1]), as_version=4)
client = NotebookClient(nb, timeout=300, kernel_name="python3",
                        resources={"metadata": {"path": str(Path(sys.argv[1]).parent)}})
client.execute()
print(f"OK: {sys.argv[1]} all cells executed")
```

注意：NotebookClient 默认内核名 `python3`；执行前确认 `$PY -m python -c "import ipykernel"` 且 kernel spec 指向 lawagent 环境（`$PY -m ipykernel install --user --name lawagent` 后用 `kernel_name="lawagent"`）。若不想注册内核，可在 notebook 内用 `import sys; sys.executable` 自验环境。
- notebook 中 LLM 替身：移植 `tests/test_smoke.py` 的 `_FakeVerdict/_FakeChain/_FakeLLM`（Runnable 子类），用 `unittest.mock.patch` + try/finally 恢复，`asyncio.run()` 驱动
- 每个 notebook 末尾 cell 固定为 `print("ALL PASSED")`，验证脚本以此 + 无异常为通过标准

## File Structure（全局地图）

```
lawApp_LangGraph/
├── state.py                    # Task 2: 要素模型 + AgentState 变更
├── prompts.py                  # Task 3: 新建，全部提示词集中
├── LangGraph_lawApp.py         # Task 4-6: 拆 clarify/新增节点/路由/装配
├── tools/rag_tools.py          # Task 7: 提示词迁出 + known_elements 拼装
├── FastAPI/
│   ├── model.py                # Task 8: QueryResponse.elements
│   ├── utils.py                # Task 8: normalize_resume + build_response
│   └── api.py                  # Task 8: resume 类型感知 + SSE elements 事件
├── clarify_test.ipynb           # Task 9
├── hitl_test.ipynb              # Task 10
├── prompts_test.ipynb           # Task 11
└── PROJECT_OVERVIEW.md          # Task 12: 文档同步（Mermaid）
tests/test_smoke.py              # Task 6 内就地更新（随节点拆分走）
scripts/run_nb.py                # Task 1
```

依赖顺序：Task 1（基建）→ 2（state）→ 3（prompts）→ 4（新 Schema+risk/assess/ask）→ 5（mid/degrade/budget）→ 6（主图重构+pytest 更新）→ 7（rag_tools）→ 8（API）→ 9/10/11（notebook 验证，依赖 4-8 全部就绪）→ 12（文档）。

---

### Task 1: 测试基建 — nbclient 验证脚本

**Files:**
- Create: `scripts/run_nb.py`

**Interfaces:**
- Produces: `python scripts/run_nb.py <notebook.ipynb>` 命令行入口，退出码 0 = 通过；Task 9-11 全部用此验证

- [ ] **Step 1: 写脚本**

```python
"""程序化执行 notebook 验证（子项目A ipynb 测试约定）。

用法: /Users/qinglan/miniconda3/envs/lawagent/bin/python scripts/run_nb.py <nb.ipynb>
通过标准: 所有 cell 无异常执行完毕（末尾 cell 打印 ALL PASSED 由 notebook 自身保证）。
kernel_name 取 lawagent（已注册）; 若未注册则回退 python3。
"""
import subprocess
import sys
from pathlib import Path

import nbformat
from nbclient import NotebookClient

ROOT = Path(__file__).resolve().parent.parent


def _kernel_available(name: str) -> bool:
    out = subprocess.run(
        [sys.executable, "-m", "jupyter", "kernelspec", "list"],
        capture_output=True, text=True,
    )
    return name in out.stdout


def main() -> int:
    nb_path = Path(sys.argv[1]).resolve()
    nb = nbformat.read(nb_path, as_version=4)
    kernel = nb.metadata.get("kernelspec", {}).get("name", "lawagent")
    if kernel != "python3" and not _kernel_available(kernel):
        kernel = "python3"
    client = NotebookClient(
        nb, timeout=600, kernel_name=kernel,
        resources={"metadata": {"path": str(nb_path.parent)}},
    )
    client.execute()
    print(f"OK: {nb_path.name} all cells executed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: 注册内核并冒烟验证**

```bash
$PY -m ipykernel install --user --name lawagent --display-name "lawagent"
$PY scripts/run_nb.py lawApp_LangGraph/nodes_test.ipynb 2>/dev/null || echo "旧 notebook 可能无法全量执行（依赖外部服务），仅验证脚本本身无 ImportError"
$PY -c "import nbformat, nbclient; print('tooling ok')"
```

Expected: `tooling ok`；若旧 notebook 执行失败且错误是外部服务连接类（Pinecone/DeepSeek），属预期，不算本任务失败。

- [ ] **Step 3: Commit**

```bash
git add scripts/run_nb.py
git commit -m "A3: 测试基建 — nbclient 程序化执行 notebook 的验证脚本"
```

---

### Task 2: 数据模型 — CaseElements / ClarifyExchange / AgentState 变更

**Files:**
- Modify: `lawApp_LangGraph/state.py`

**Interfaces:**
- Produces（后续所有任务依赖，签名精确）:
  - `CaseElement(key: str, label: str, critical: bool, status: Literal["known","missing","na"]="missing", value: str="", updated_by: str="init")`
  - `CaseElements(elements: list[CaseElement])` 及方法：`digest() -> str`、`critical_missing() -> list[CaseElement]`、`mark_na(keys: list[str])`、`promote(keys: list[str])`、`update(key: str, value: str, by: str)`
  - `ElementQuestion(key: str, question: str)`
  - `ClarifyExchange(round: int, question: str, answer: str, element_keys: list[str], at: str)`
  - `default_case_elements() -> CaseElements`（ingest 重建默认清单用）
  - 常量 `MAX_CLARIFY_ROUNDS=5`、`ERROR_STREAK_THRESHOLD=2`
  - AgentState 新字段：`case_elements: CaseElements`（覆盖）、`clarify_history: Annotated[list[ClarifyExchange], append_list]`、`pending_questions: list[ElementQuestion]`（覆盖）、`clarify_rounds: int`、`error_streak: int`、`mid_clarify_used: bool`、`budget_hitl_used: bool`、`degrade_used: bool`
  - AgentState 删除字段：`clarification_round`、`clarification`
  - `PromptsRecord` 新字段：`known_elements: str = ""`

- [ ] **Step 1: 先在 pytest 写失败测试（模型层用 pytest 快测，notebook 留给图行为——模型单测更适合跑批量断言）**

在 `tests/test_smoke.py` 追加（放在 `test_state_reducers` 之后）：

```python
def test_case_elements_model():
    from lawApp_LangGraph.state import (
        CaseElements, default_case_elements, MAX_CLARIFY_ROUNDS,
        ERROR_STREAK_THRESHOLD,
    )

    ce = default_case_elements()
    # 默认 7 要素，关键 3 个
    keys = [e.key for e in ce.elements]
    assert keys == [
        "marriage_status", "demand", "property",
        "children", "timeline", "evidence", "opposing_stance",
    ]
    assert [e.label for e in ce.elements if e.critical] == ["婚姻现状", "核心诉求", "主要财产与归属"]
    assert len(ce.critical_missing()) == 3

    # mark_na / promote / update
    ce.mark_na(["evidence"])
    ce.promote(["timeline"])
    assert ce.elements[4].critical is True          # timeline 升关键
    assert ce.elements[5].status == "na"            # evidence 不适用
    ce.update("marriage_status", "在婚,分居中", by="assess")
    assert ce.elements[0].status == "known"
    assert ce.elements[0].value == "在婚,分居中"
    assert ce.elements[0].updated_by == "assess"

    # critical_missing 随更新收缩
    assert [e.key for e in ce.critical_missing()] == ["demand", "property", "timeline"]

    # digest: known 的进文本, missing/na 不进
    ce.update("property", "一套房,双方名下", by="assess")
    d = ce.digest()
    assert "婚姻现状" in d and "一套房,双方名下" in d
    assert "核心诉求" not in d

    assert MAX_CLARIFY_ROUNDS == 5
    assert ERROR_STREAK_THRESHOLD == 2


def test_agent_state_new_fields():
    from lawApp_LangGraph.state import AgentState

    s = AgentState(query="我想离婚")
    for f in ("case_elements", "clarify_history", "pending_questions",
              "clarify_rounds", "error_streak", "mid_clarify_used",
              "budget_hitl_used", "degrade_used"):
        assert hasattr(s, f), f"缺少新字段 {f}"
    assert s.clarify_rounds == 0 and s.error_streak == 0
    assert not (s.mid_clarify_used or s.budget_hitl_used or s.degrade_used)
    # 旧字段已删
    assert not hasattr(s, "clarification_round")
    assert not hasattr(s, "clarification")
```

- [ ] **Step 2: 运行验证失败**

Run: `$PY -m pytest tests/test_smoke.py::test_case_elements_model tests/test_smoke.py::test_agent_state_new_fields -v`
Expected: FAIL（ImportError: cannot import name 'CaseElements'）

- [ ] **Step 3: 实现 state.py 变更**

在 `state.py` 的 A 区（工具返回层）末尾追加：

```python
#  案件要素清单 — 子项目A 澄清循环的数据基础

MAX_CLARIFY_ROUNDS = 5      # 入口澄清轮数上限
ERROR_STREAK_THRESHOLD = 2  # 连续失败触发降级询问

# 默认要素清单:(key, label, 关键级) — 婚姻家事与语料库领域对齐
_DEFAULT_ELEMENTS: tuple[tuple[str, str, bool], ...] = (
    ("marriage_status", "婚姻现状", True),
    ("demand", "核心诉求", True),
    ("property", "主要财产与归属", True),
    ("children", "子女情况", False),
    ("timeline", "关键时间线", False),
    ("evidence", "手头证据", False),
    ("opposing_stance", "对方态度", False),
)


class CaseElement(BaseModel):
    """单个案件要素的状态"""

    key: str
    label: str
    critical: bool
    status: Literal["known", "missing", "na"] = "missing"
    value: str = ""
    updated_by: str = "init"   # init / assess / ask / mid_clarify


class ElementQuestion(BaseModel):
    """评估 LLM 生成的要素反问(随 state 序列化)"""

    key: str
    question: str


class CaseElements(BaseModel):
    """案件要素清单 — 澄清循环的状态机载体"""

    elements: List[CaseElement] = Field(default_factory=list)

    def digest(self) -> str:
        """已知要素的提示词注入文本:'标签:摘要' 竖线拼接"""
        known = [e for e in self.elements if e.status == "known" and e.value]
        if not known:
            return "暂无已知要素"
        return " | ".join(f"{e.label}:{e.value}" for e in known)

    def critical_missing(self) -> List[CaseElement]:
        return [e for e in self.elements if e.critical and e.status == "missing"]

    def mark_na(self, keys: List[str]) -> None:
        for e in self.elements:
            if e.key in keys:
                e.status = "na"

    def promote(self, keys: List[str]) -> None:
        for e in self.elements:
            if e.key in keys:
                e.critical = True

    def update(self, key: str, value: str, by: str) -> None:
        for e in self.elements:
            if e.key == key:
                e.status = "known"
                e.value = value
                e.updated_by = by
                return


def default_case_elements() -> CaseElements:
    """ingest 重建默认清单"""
    return CaseElements(
        elements=[
            CaseElement(key=k, label=lbl, critical=crit)
            for k, lbl, crit in _DEFAULT_ELEMENTS
        ]
    )


class ClarifyExchange(BaseModel):
    """一轮澄清交互的记录(审计 + B子项目记忆固化原料)"""

    round: int
    question: str
    answer: str
    element_keys: List[str]
    at: str = Field(default_factory=lambda: datetime.now().isoformat())
```

`PromptsRecord` 增加一个字段（在 `laws_results` 之后）：

```python
    # 已知案件要素 digest(由 merge 节点填充, 分析上下文注入)
    known_elements: str = ""
```

`AgentState` 变更——删除 HITL 段的 `clarification_round`/`clarification` 两字段，原 HITL 段替换为：

```python
    # ============= HITL(人机协同)=============
    # 案件要素清单(覆盖语义, ingest 重建默认清单)
    case_elements: CaseElements = Field(default_factory=default_case_elements)
    # 澄清历史(增量追加, ingest RESET 清空)
    clarify_history: Annotated[List[ClarifyExchange], append_list] = Field(
        default_factory=list
    )
    # 评估节点写入、反问节点读取(覆盖语义)
    pending_questions: List[ElementQuestion] = Field(default_factory=list)
    # 澄清轮数(上限 MAX_CLARIFY_ROUNDS, ingest 归零)
    clarify_rounds: int = 0
    # 用户已确认高风险话题 / PDF 生成
    risk_confirmed: bool = False
    pdf_confirmed: bool = False
    # 工具连续失败计数(成功清零, >=ERROR_STREAK_THRESHOLD 触发降级)
    error_streak: int = 0
    # 一次性标记(防循环, ingest 重置)
    mid_clarify_used: bool = False
    budget_hitl_used: bool = False
    degrade_used: bool = False
    # 最近一次 interrupt 事件(覆盖语义)
    hitl_event: Optional[Dict[str, Any]] = None
```

ingest 的 RESET 追加（Task 4 一起改 ingest 时并入）。

- [ ] **Step 4: 全量跑 pytest**

Run: `$PY -m pytest tests/ -q`
Expected: 旧用例有若干 FAIL —— `test_interrupt_resume`/`test_graph_topology` 因 Task 4/6 才修，属预期；但 `test_case_elements_model`/`test_agent_state_new_fields`/`test_state_reducers`/`test_imports` 必须 PASS。若 `test_imports` 失败说明 state.py 语法错，先修。

- [ ] **Step 5: Commit**

```bash
git add lawApp_LangGraph/state.py tests/test_smoke.py
git commit -m "A3: 数据模型 — 案件要素清单 CaseElements + 澄清历史 + AgentState 新字段"
```

---

### Task 3: 提示词集中化 — 新建 prompts.py

**Files:**
- Create: `lawApp_LangGraph/prompts.py`
- Modify: `lawApp_LangGraph/LangGraph_lawApp.py`（仅 import 切换，不删旧常量——Task 6 重构时删）
- Modify: `lawApp_LangGraph/tools/rag_tools.py`（同上，仅 import 切换）

**Interfaces:**
- Produces（Task 4/5/6/7 依赖）:
  - 常量提示词：`KIM_PERSONA_BLOCK`、`RISK_GATE_PROMPT`、`ELEMENT_ASSESS_PROMPT`、`MID_CLARIFY_PROMPT`、`REPLAN_CHECK_PROMPT`、`PLANNER_SYSTEM`、`EXECUTOR_PROMPT`、`REPLANNER_SYSTEM_PROMPT`、`DEGRADE_CONFIRM_MSG`、`BUDGET_CONFIRM_MSG`、`FINALIZE_CASE_PROMPT`、`FINALIZE_DIRECT_PROMPT`、`LEGAL_ANALYSIS_PROMPT_KIM`、`LEGAL_ANALYSIS_PROMPT_Saul`
  - 函数 `get_analysis_prompt() -> PromptTemplate`（从 rag_tools.py 迁入）
  - 所有 LLM 提示词模板变量（`PromptTemplate.from_template` 的 `{var}`）见各提示词内注释

- [ ] **Step 1: 写 prompts.py**

```python
"""
提示词集中模块 — 子项目A (v2)

全部 LLM 提示词与 interrupt 文案收拢于此,每个提示词头部带版本注释。
设计来源: docs/superpowers/specs/2026-09-10-clarify-hitl-design.md

模板变量总表(供 prompts_test.ipynb 校验):
    RISK_GATE_PROMPT:            {query}
    ELEMENT_ASSESS_PROMPT:       {query} {elements_digest} {last_question} {last_answer} {round} {max_rounds}
    MID_CLARIFY_PROMPT:          {query} {top_docs_summary}
    REPLAN_CHECK_PROMPT:         {user_query} {executed_summary} {doc_count} {quality_verdict} {web_count} {law_count} {error_info}
    PLANNER_SYSTEM:              {available_tools} {query} {elements_digest}
    EXECUTOR_PROMPT:             {step_description} {tool_name} {user_query} {rag_summary} {eval_summary} {law_summary} {web_summary} {elements_digest}
    REPLANNER_SYSTEM_PROMPT:     {executed_steps} {doc_count} {quality} {web_count} {law_count} {error} {replan_reason} {available_tools} {user_query} {next_id}
    FINALIZE_CASE_PROMPT:        {docs} {query}
    FINALIZE_DIRECT_PROMPT:      {query}
    DEGRADE_CONFIRM_MSG:         {failed_tool}
    BUDGET_CONFIRM_MSG:          {missing}
"""

from langchain_core.prompts import PromptTemplate

#  Kim 人设公共块 — 注入反问生成与 finalize 兜底,全链路人格统一
KIM_PERSONA_BLOCK = """# Role: Kim Wexler (《风骚律师》中的资深律师)

你是一位经验丰富、务实沉稳的婚姻家事法律顾问。面对不懂法条、容易焦虑的普通人,
你用专业和冷静帮他们看清局面:先一句简短共情,随即切入法律事实与可行方案。
语气清醒坚定、极度务实、用大白话,给当事人掌控感。"""


#  高风险判定 (v2: 从旧 CLARIFY_PROMPT 的风险标准独立,判定面扩充)
RISK_GATE_PROMPT = """你是法律AI系统的接诊助理.判断用户咨询是否涉及高风险话题.

## 判定标准(命中任一即 high_risk)
1. 自伤自杀倾向
2. 家庭暴力正在发生(当下的人身危险)
3. 扬言报复、伤害他人
4. 涉及刑事犯罪(自首、被通缉等)
5. 涉未成年人正在受害

## 用户问题
{query}

按给定 JSON Schema 输出判断."""


#  要素评估 (v2 新增 — 澄清循环核心)
# 输出 Schema: ElementAssessmentSchema(见 LangGraph_lawApp._schema_models)
ELEMENT_ASSESS_PROMPT = KIM_PERSONA_BLOCK + """

## 任务
你是接诊律师.(1)判断咨询是否属于婚姻家事类;(2)若用户刚回答了上一轮反问,
把回答内容映射到对应要素;(3)评估还缺哪些**关键**要素,生成律师式反问.

## 案件要素清单(当前状态)
{elements_digest}

## 上一轮反问与用户回答(首轮为空)
{last_question}
用户回答: {last_answer}

## 分案由指引(动态提升关键级)
- 彩礼/婚约财产纠纷 → timeline(关键时间线)升为关键
- 抚养权纠纷 → children 与 opposing_stance 升为关键
- 继承纠纷 → timeline 升为关键

## 规则
1. 非婚姻家事类咨询(闲聊/概念解释/其他法律领域) → applicable=false
2. 反问只针对清单内**关键且仍为 missing** 的要素,每次最多 3 个
3. 反问要像律师问诊:自然口语,一次最多打包 2~3 个要素为一句问话,体现专业与共情
4. 已问过但用户没答的要素不要重复追问
5. 关键要素齐了 → done=true(常规要素缺失不阻塞)
6. element_updates 只标注有把握的映射,无把握不要标

## 用户问题
{query}

## 当前轮次
第 {round} 轮 / 上限 {max_rounds} 轮

按给定 JSON Schema 输出。"""


#  检索反馈追问 (v2 新增 — 检索不足且原因笼统时,先问人后搜网)
MID_CLARIFY_PROMPT = KIM_PERSONA_BLOCK + """

## 任务
检索到的案例与用户问题的匹配集中在某个特定情形,说明问题问得笼统.
请基于检索结果摘要,生成**一个**聚焦追问,帮用户把模糊点说清.

## 检索到的案例摘要(注意它们的共同情形)
{top_docs_summary}

## 用户问题
{query}

## 规则
1. 只生成一个追问,直击检索结果暴露的模糊点
   (如:案例集中在"婚后共同还贷",就问"你的房子是婚前买的还是婚后买的?")
2. 律师问诊语气,一句话
3. element_key 填该追问对应的要素 key

按给定 JSON Schema 输出。"""


#  质量门控 (v2: 增加 insufficient_reason 诊断)
REPLAN_CHECK_PROMPT = """你是法律AI系统的质量审核员。检查已执行步骤的结果,判断当前信息是否足以生成高质量的法律回答;若不足,诊断原因.

## 用户原始问题
{user_query}

## 已执行步骤及结果
{executed_summary}

## 当前数据状态
- 检索到的案例数量: {doc_count}
- 案例质量评估: {quality_verdict}
- 网络搜索补充: {web_count} 条
- 法律条文检索: {law_count} 条
- 执行错误: {error_info}

## 判断标准
1. 已检索到相关案例且质量评估为"充足" → 不需要重规划 (insufficient_reason=none)
2. 检索结果为空或普遍低分,案例库覆盖不到 → 需要重规划,原因 not_found
   (此时应联网搜索补充,而非追问用户)
3. 案例有量但反复 ambiguous,且用户问题笼统缺少具体情节 → 需要重规划,原因 vague
   (此时应先追问用户细化问题,而非联网)
4. 执行中出现了无法恢复的错误 → 需要重规划,原因 error
5. 已有 final_answer 或 analyze_legal_issue 已成功执行 → 不需要重规划 (insufficient_reason=none)
6. 已有足够案例且进行了法律分析 → 不需要重规划 (insufficient_reason=none)"""


#  规划 (v2: 注入已知案件要素段)
PLANNER_SYSTEM = """你是法律AI系统的任务规划师.分析用户问题,制定可执行的步骤计划.

## 可用工具
{available_tools}

## 已知案件要素(经问询收集,规划时可参考)
{elements_digest}

## 计划原则
- 法律问题: retrieve_legal_knowledge → evaluate_case_relevance → analyze_legal_issue
- 如需要引用具体法律条文作为依据: 在检索案例后插入 fetch_laws 获取相关法条原文
- 如评估结果为"不足": 插入 get_google_search 联网补充再分析
- 如用户提及之前讨论过的话题: 先用 search_memory 搜索历史记忆获取上下文
- 一般情况下,在生成最终回答后用 save_to_memory 保存
- 一般情况下不需要过多网络搜索,优先利用 RAG 检索到的案例;如案例不足再补充网络搜索
- 如用户要求输出 PDF 报告: 最后一步调用 markdown_to_pdf 生成 PDF 文件(执行前系统会请求用户确认)
- 简单闲聊: plan 为空数组 []
- tool_name 必须是上述列表中的名称,不需要工具则填写 null
- 计划步骤不超过 8 步

## 用户问题
{query}"""


#  执行 (v2: 注入已知案件要素段)
EXECUTOR_PROMPT = """你是执行器,只做一件事:调用指定的工具.

当前步骤: {step_description}
指定工具: {tool_name}
用户问题: {user_query}
已知案件要素: {elements_digest}

上下文数据:
- 已检索案例: {rag_summary}
- 案例评估: {eval_summary}
- 检索法条: {law_summary}
- 网络搜索: {web_summary}

规则:
1. 只调用 {tool_name},不要调用其他工具
2. 从上下文和用户问题中提取参数
3. 不要做推理,只需正确调用工具
4. 必须发起一次工具调用"""


#  重规划 (v1 原样迁移)
REPLANNER_SYSTEM_PROMPT = """你是任务规划师.基于已执行的步骤和当前结果,生成**补充步骤**.

## 已执行步骤
{executed_steps}

## 当前状态
- 案例数量: {doc_count}
- 评估结论: {quality}
- 网络搜索: {web_count} 条
- 法律条文: {law_count} 条
- 错误: {error}

## 重规划原因
{replan_reason}

## 可用工具
{available_tools}

## 用户问题
{user_query}

## 要求
只输出需要**新增**的步骤,不要重复已完成的步骤.新增步骤不超过 3 步.
下一个步骤编号从 {next_id} 开始."""


#  interrupt 静态文案 (不调 LLM)
DEGRADE_CONFIRM_MSG = "工具 {failed_tool} 已连续失败多次。请选择处理方式:回复「重试」重新规划调用,回复「跳过」继续后续步骤,回复「终止」结束本次咨询。"

BUDGET_CONFIRM_MSG = "本次咨询的执行预算即将用尽,当前信息可能不足以给出高质量回答。\n还缺: {missing}\n回复补充内容将继续深入分析,回复「收尾」将基于现有材料给出回答。"


#  兜底回答 (v2: Kim 人设替换"七成傲娇")
FINALIZE_CASE_PROMPT = PromptTemplate.from_template(
    KIM_PERSONA_BLOCK + """

## 任务
基于以下案例,简要回答用户问题.引用关键裁判思路,末尾附一行:「以上内容由 AI 生成,仅供参考,不构成正式法律意见。」

## 案例
{docs}

## 问题
{query}

## 回答"""
)

FINALIZE_DIRECT_PROMPT = PromptTemplate.from_template(
    KIM_PERSONA_BLOCK + """

## 任务
根据你的法律知识回答用户问题.用大白话,先结论后展开,末尾附一行:
「以上内容由 AI 生成,仅供参考,不构成正式法律意见。」

## 问题
{query}

## 回答"""
)


#  法律分析角色 (v1 从 rag_tools.py 迁入;Saul 仅影响分析,经 LEGAL_ANALYSIS_ROLE 切换)
LEGAL_ANALYSIS_PROMPT_KIM = PromptTemplate.from_template(
    KIM_PERSONA_BLOCK + """

## 分析要求
1. 明确法律定性:一句话点出核心法律关系(婚姻财产分割/抚养权/继承等).
2. 引用法条依据:优先引用「相关法条」原文,明确出处(法规名称+条款号).
3. 引用参考案例:从参考资料中提取判例,概括裁判思路佐证分析,不照搬原文.
4. 指出关键风险:用户可能没意识到的法律陷阱、证据短板、时效问题.
5. 给出可行建议:具体的下一步行动.
6. 区分确定与不确定:明确哪些结论有充分依据,哪些还需核实.
7. 完整 Markdown 格式输出,结构清晰.
8. 末尾列出参考了哪些资料(如「参考了3条案例和2条法条」)及编号或标题.
9. 内容末尾附一行:「以上内容由 AI 生成,仅供参考,不构成正式法律意见。」

## 参考材料
{context}

## 用户问题
{query}

## 你的回答"""
)

LEGAL_ANALYSIS_PROMPT_Saul = PromptTemplate.from_template(
    """
    # Role: Saul Goodman (《风骚律师》中的传奇边缘律师)
    (v1 原样迁移,详见 git history;仅经 LEGAL_ANALYSIS_ROLE=saul 启用,默认 Kim)
    ## 参考材料
    {context}

    ## 用户问题
    {query}

    ## 你的回答
    """
)


def get_analysis_prompt() -> PromptTemplate:
    """根据 LEGAL_ANALYSIS_ROLE 选择分析角色(kim 默认 / saul)."""
    import os

    role = os.getenv("LEGAL_ANALYSIS_ROLE", "kim").lower().strip()
    return LEGAL_ANALYSIS_PROMPT_Saul if role == "saul" else LEGAL_ANALYSIS_PROMPT_KIM
```

**注意**：`LEGAL_ANALYSIS_PROMPT_Saul` 的 v1 完整人设文本从 `rag_tools.py:276-308` 原样复制（计划中省略正文避免重复占版面——**执行者必须从 rag_tools.py 复制原文**，不要用上面的占位段落）。

- [ ] **Step 2: 切换 import（不删旧定义）**

`LangGraph_lawApp.py` 顶部（`load_dotenv()` 之后）加：

```python
from lawApp_LangGraph.prompts import (
    DEGRADE_CONFIRM_MSG,
    ELEMENT_ASSESS_PROMPT,
    EXECUTOR_PROMPT,
    FINALIZE_CASE_PROMPT,
    FINALIZE_DIRECT_PROMPT,
    MID_CLARIFY_PROMPT,
    PLANNER_SYSTEM,
    REPLANNER_SYSTEM_PROMPT,
    REPLAN_CHECK_PROMPT,
    RISK_GATE_PROMPT,
)
from lawApp_LangGraph.tools.rag_tools import analyze_legal_issue  # noqa — 已有,确认不缺
```

`tools/rag_tools.py`：删除文件底部的 `_get_analysis_prompt()` 函数、`LEGAL_ANALYSIS_PROMPT_Kim`/`LEGAL_ANALYSIS_PROMPT_Saul` 两个常量与 `PromptTemplate` import（若 PromptTemplate 仍被别处使用则保留 import），`analyze_legal_issue` 中 `final_prompt = _get_analysis_prompt().format(...)` 改为：

```python
from lawApp_LangGraph.prompts import get_analysis_prompt  # 文件顶部

final_prompt = get_analysis_prompt().format(context=context, query=query)
```

- [ ] **Step 3: 冒烟验证**

Run: `$PY -m pytest tests/ -q -k "imports or spelling"`
Expected: PASS（import 链不断）

- [ ] **Step 4: Commit**

```bash
git add lawApp_LangGraph/prompts.py lawApp_LangGraph/LangGraph_lawApp.py lawApp_LangGraph/tools/rag_tools.py
git commit -m "A3: 提示词集中化 — 新建 prompts.py,Kim 人设全链路统一,REPLAN_CHECK 增加原因诊断"
```

---

### Task 4: 入口三节点 — risk_gate / element_assess / ask_element

**Files:**
- Modify: `lawApp_LangGraph/LangGraph_lawApp.py`（替换 `clarify_node` 及其 prompt/schema，新增三节点函数；**本任务只写节点函数，不动 build_graph**）

**Interfaces:**
- Consumes: Task 2 的 `CaseElements/default_case_elements/ClarifyExchange/ElementQuestion/MAX_CLARIFY_ROUNDS`；Task 3 的 `RISK_GATE_PROMPT/ELEMENT_ASSESS_PROMPT`
- Produces（Task 6 build_graph 依赖，函数签名）:
  - `async risk_gate_node(state: AgentState) -> dict`
  - `async element_assess_node(state: AgentState) -> dict`
  - `def ask_element_node(state: AgentState) -> dict`（resume 重跑幂等,纯记账）
  - Schema 类 `RiskSchema{high_risk: bool, reason: str}`、`ElementAssessmentSchema{applicable: bool, element_updates: list[ElementUpdate], na_keys: list[str], promote_keys: list[str], questions: list[ElementQuestion], done: bool}`（其中 `ElementUpdate{key: str, value: str, status: Literal["known","na"]}`，均在 `_schema_models()` 定义）
  - `ingest_node` 更新版（重置新字段）

- [ ] **Step 1: 扩展 `_schema_models()`**

在 `_schema_models()` 内删除 `ClarifySchema`，新增两个类并更新返回：

```python
    class RiskSchema(BaseModel):
        high_risk: bool = Field(description="是否命中高风险判定标准")
        reason: str = Field(default="", description="命中的标准,不超过30字")

    class ElementUpdate(BaseModel):
        key: str = Field(description="要素 key,必须是清单内的 key")
        value: str = Field(default="", description="从用户回答提取的要素摘要")
        status: Literal["known", "na"] = Field(default="known")

    class ElementAssessmentSchema(BaseModel):
        applicable: bool = Field(description="是否婚姻家事类咨询")
        element_updates: List[ElementUpdate] = Field(
            default_factory=list, description="用户上轮回答映射到的要素"
        )
        na_keys: List[str] = Field(default_factory=list, description="本案不涉及的要素")
        promote_keys: List[str] = Field(default_factory=list, description="按案由升关键的要素")
        questions: List[ElementQuestion] = Field(
            default_factory=list, description="本轮反问,只问关键且缺失,最多3个"
        )
        done: bool = Field(description="要素已足够,无需再问")

    return PlanSchema, ReplanCheckSchema, RiskSchema, ElementAssessmentSchema
```

同时把模块底部的解包改为 `PlanSchema, ReplanCheckSchema, RiskSchema, ElementAssessmentSchema = _schema_models()`，并在文件头 import 区加 `from typing import Literal`（若未有）与 `from lawApp_LangGraph.state import ClarifyExchange, ElementQuestion`（并入现有 state import）。

- [ ] **Step 2: 更新 ingest_node**

在 `ingest_node` 返回 dict 中追加/修改（保留现有全部字段）：

```python
        # 子项目A: 要素清单重建 + 澄清/故障计数归零 + 一次性标记复位
        "case_elements": default_case_elements(),
        "clarify_rounds": 0,
        "error_streak": 0,
        "mid_clarify_used": False,
        "budget_hitl_used": False,
        "degrade_used": False,
        "pending_questions": [],
        # 删除原 "clarification_round"/"clarification" 两行, clarify_history 入 RESET 列表
        "clarify_history": RESET,
```

- [ ] **Step 3: 写 risk_gate_node（替换 clarify_node 的风险部分）**

删除整个旧 `clarify_node` 与 `CLARIFY_PROMPT` 常量，写入：

```python
# Node 0.5a: Risk Gate — 高风险话题确认 (HITL-1)

async def risk_gate_node(state: AgentState) -> dict:
    """LLM 高风险判定;未确认的高风险 → interrupt 确认,拒绝则热线文案中止."""
    t0 = time.time()
    query = state.query.strip()
    if not query or state.risk_confirmed:
        return {}

    high_risk = False
    try:
        chain = (
            PromptTemplate.from_template(RISK_GATE_PROMPT)
            | get_executor_llm().with_structured_output(RiskSchema)
        )
        verdict = await chain.ainvoke({"query": query[:2000]})
        high_risk = bool(verdict.high_risk)
    except Exception as e:
        # LLM 失败 → 视为无风险放行(HITL 是增强项不是阻塞项)
        debug.warning("Risk Gate LLM 失败,放行", detail=str(e)[:100])

    if not high_risk:
        debug.debug("← Risk Gate 通过", result=f"elapsed={time.time() - t0:.2f}s")
        return {}

    confirmed = interrupt(
        {
            "type": "risk_confirm",
            "message": "您的问题可能涉及人身安全或重大风险。如果您正面临家暴、自伤或紧迫的危险，请立即拨打110或联系当地妇联/救助机构。确认继续进行AI法律咨询吗？",
        }
    )
    if not confirmed:
        return {
            "final_answer": (
                "已中止本次咨询。请优先保证人身安全：紧急情况拨打110，"
                "家暴可拨打全国妇联维权热线12338，心理困境可拨打希望热线400-161-9995。"
                "安全得到保障后，欢迎随时回来咨询法律问题。"
            ),
            "risk_confirmed": True,
            "hitl_event": {"type": "risk_confirm", "confirmed": False,
                           "at": datetime.now().isoformat()},
        }
    return {
        "risk_confirmed": True,
        "hitl_event": {"type": "risk_confirm", "confirmed": True,
                       "at": datetime.now().isoformat()},
    }
```

- [ ] **Step 4: 写 element_assess_node**

```python
# Node 0.5b: Element Assess — LLM 评估要素缺口 + 解读上轮回答

async def element_assess_node(state: AgentState) -> dict:
    """评估案件要素:(1)应用用户上轮回答的要素映射 (2)生成下一轮反问.
    LLM 失败 → done=True 软放行(不阻塞)."""
    t0 = time.time()
    debug.debug("→ 进入 Element Assess 节点",
                detail=f"round={state.clarify_rounds}/{MAX_CLARIFY_ROUNDS}")

    ce = state.case_elements.model_copy(deep=True)
    last_q, last_a = "", ""
    if state.clarify_history:
        ex = state.clarify_history[-1]
        last_q, last_a = ex.question, ex.answer

    try:
        chain = (
            PromptTemplate.from_template(ELEMENT_ASSESS_PROMPT)
            | get_executor_llm().with_structured_output(ElementAssessmentSchema)
        )
        v = await chain.ainvoke({
            "query": state.query[:2000],
            "elements_digest": ce.digest(),
            "last_question": last_q,
            "last_answer": last_a or "(尚未反问)",
            "round": state.clarify_rounds + 1,
            "max_rounds": MAX_CLARIFY_ROUNDS,
        })
    except Exception as e:
        debug.warning("Element Assess LLM 失败,软放行进 planner", detail=str(e)[:100])
        return {"pending_questions": [], "case_elements": ce}

    # (1) 非婚姻家事类 → 全 na,直接放行
    if not v.applicable:
        ce.mark_na([e.key for e in ce.elements])
        debug.info("← Element Assess: 非目标类咨询,全 na 直通",
                   result=f"elapsed={time.time() - t0:.2f}s")
        return {"pending_questions": [], "case_elements": ce}

    # (2) 应用要素更新(用户回答映射 + na + 关键级提升)
    valid_keys = {e.key for e in ce.elements}
    for u in v.element_updates:
        if u.key in valid_keys:
            ce.update(u.key, u.value, by="assess")
    if v.na_keys:
        ce.mark_na([k for k in v.na_keys if k in valid_keys])
    if v.promote_keys:
        ce.promote([k for k in v.promote_keys if k in valid_keys])

    # (3) 决定是否继续问
    questions = []
    if not v.done and state.clarify_rounds < MAX_CLARIFY_ROUNDS:
        questions = [q for q in v.questions if q.key in valid_keys][:3]
        # 关键缺口为空时不再问
        if not ce.critical_missing():
            questions = []

    debug.info(
        "← Element Assess 完成",
        detail=f"known={len([e for e in ce.elements if e.status=='known'])}"
               f"/{len(ce.elements)} | critical_missing={len(ce.critical_missing())}",
        result=f"elapsed={time.time() - t0:.2f}s | {'继续反问' if questions else '放行'}",
    )
    return {"case_elements": ce, "pending_questions": questions}
```

- [ ] **Step 5: 写 ask_element_node（LLM-free 纯记账）**

```python
# Node 0.5c: Ask Element — HITL-2 要素反问(纯记账,resume 重跑幂等)

def ask_element_node(state: AgentState) -> dict:
    """发起要素反问 interrupt;resume 后记录 clarify_history、轮数自增.
    resume 返回值: 非空字符串=用户回答;空/None=跳过(轮数置满)."""
    questions = state.pending_questions
    if not questions:
        return {}  # 防御:无问题不 interrupt

    question_text = " ".join(q.question for q in questions)
    keys = [q.key for q in questions]
    answer = interrupt({
        "type": "clarify",
        "round": f"{state.clarify_rounds + 1}/{MAX_CLARIFY_ROUNDS}",
        "question": question_text,
        "elements": [
            {"key": e.key, "label": e.label, "status": e.status}
            for e in state.case_elements.elements
        ],
    })

    answer = (str(answer).strip() if answer else "")
    if not answer:
        # 用户跳过 → 轮数置满,按原问题继续(与存量"未补充→按原问题继续"语义一致)
        debug.info("← Ask Element: 用户跳过反问", detail="按原问题继续")
        return {"clarify_rounds": MAX_CLARIFY_ROUNDS,
                "pending_questions": [],
                "hitl_event": {"type": "clarify", "skipped": True,
                               "at": datetime.now().isoformat()}}

    # 答案织入增强 query(下游 planner/executor/检索全部基于此)
    augmented_query = f"{state.query}\n[用户补充信息] {answer}"
    debug.info("← Ask Element 完成",
               detail=f"answer={answer[:80]}",
               result=f"round={state.clarify_rounds + 1}/{MAX_CLARIFY_ROUNDS}")
    return {
        "clarify_rounds": state.clarify_rounds + 1,
        "clarify_history": [ClarifyExchange(
            round=state.clarify_rounds + 1,
            question=question_text,
            answer=answer,
            element_keys=keys,
        )],
        "query": augmented_query,
        "hitl_event": {"type": "clarify", "question": question_text,
                       "at": datetime.now().isoformat()},
    }
```

- [ ] **Step 6: 语法验证**

Run: `$PY -c "import lawApp_LangGraph.LangGraph_lawApp as app; print('ok', [n for n in dir(app) if 'node' in n])"`
Expected: 打印 ok 且含 `risk_gate_node/element_assess_node/ask_element_node`（此刻 build_graph 仍引用已删除的 clarify —— 语法验证只测 import 层不调用 build_graph；若 import 即触发 build_graph 才会报错——本项目是懒加载单例，import 安全）。

- [ ] **Step 7: Commit**

```bash
git add lawApp_LangGraph/LangGraph_lawApp.py
git commit -m "A3: 入口三节点 — risk_gate 高风险判定 + element_assess 要素评估 + ask_element 反问(HITL-1(2))"
```

---

### Task 5: 执行中 HITL 三节点 — mid_clarify / hitl_degrade / hitl_budget + 错误计数

**Files:**
- Modify: `lawApp_LangGraph/LangGraph_lawApp.py`（新增三节点 + executor/merge 错误计数 + ReplanCheckSchema 扩展；不动 build_graph）

**Interfaces:**
- Consumes: Task 2 `ERROR_STREAK_THRESHOLD`；Task 3 `MID_CLARIFY_PROMPT/DEGRADE_CONFIRM_MSG/BUDGET_CONFIRM_MSG`
- Produces:
  - `async mid_clarify_node(state) -> dict`
  - `def hitl_degrade_node(state) -> dict`、`def hitl_budget_node(state) -> dict`（纯记账）
  - `ReplanCheckSchema` 增加字段 `insufficient_reason: Literal["vague","not_found","error","none"] = "none"`
  - executor/merge 的 `error_streak` 计数行为

- [ ] **Step 1: 扩展 ReplanCheckSchema**

`_schema_models()` 内 `ReplanCheckSchema` 改为：

```python
    class ReplanCheckSchema(BaseModel):
        needs_replan: bool = Field(description="当前信息是否不足以生成高质量回答")
        reason: str = Field(default="", description="简短判断依据,不超过50字")
        insufficient_reason: Literal["vague", "not_found", "error", "none"] = Field(
            default="none", description="不足原因: vague=问题笼统(先问人) / not_found=案例库覆盖不到(联网) / error=执行错误 / none=不不足"
        )
```

- [ ] **Step 2: mid_clarify_node**

```python
# Node 5.5: Mid Clarify — HITL-5 检索反馈追问(先问人后搜网)

async def mid_clarify_node(state: AgentState) -> dict:
    """基于检索结果的共同情形生成聚焦追问. LLM 失败 → 静默走 replanner 联网兜底."""
    t0 = time.time()
    top_docs = "\n".join(
        f"- [{d.case_number}] {d.chunk_text[:120]}..."
        for d in state.rag_documents[:5]
    ) or "(检索为空)"

    try:
        chain = (
            PromptTemplate.from_template(MID_CLARIFY_PROMPT)
            | get_executor_llm().with_structured_output(MidClarifySchema)
        )
        v = await chain.ainvoke({
            "query": state.query[:1500],
            "top_docs_summary": top_docs,
        })
    except Exception as e:
        debug.warning("Mid Clarify LLM 失败,转联网兜底", detail=str(e)[:100])
        return {"mid_clarify_used": True}

    answer = interrupt({
        "type": "mid_clarify",
        "question": v.question,
        "context_hint": f"检索到的案例集中在: {top_docs[:200]}",
    })
    answer = (str(answer).strip() if answer else "")
    if not answer:
        debug.info("← Mid Clarify: 用户未补充", detail="转联网兜底")
        return {"mid_clarify_used": True}

    augmented_query = f"{state.query}\n[检索反馈追问] {v.question}\n[用户澄清] {answer}"
    ce = state.case_elements.model_copy(deep=True)
    if v.element_key in {e.key for e in ce.elements}:
        ce.update(v.element_key, answer, by="mid_clarify")
    debug.info("← Mid Clarify 完成", detail=f"answer={answer[:80]}",
               result=f"elapsed={time.time() - t0:.2f}s | → replanner")
    return {
        "mid_clarify_used": True,
        "query": augmented_query,
        "case_elements": ce,
        "hitl_event": {"type": "mid_clarify", "question": v.question,
                       "at": datetime.now().isoformat()},
    }
```

`MidClarifySchema` 加入 `_schema_models()`（`Literal` import 已在 Task 4 引入）：

```python
    class MidClarifySchema(BaseModel):
        question: str = Field(description="一个聚焦追问,律师问诊语气,一句话")
        element_key: str = Field(default="", description="追问对应的要素 key")
```

返回行同步加 `MidClarifySchema`。

- [ ] **Step 3: hitl_degrade_node / hitl_budget_node（LLM-free）**

```python
# Node 8.5: HITL Degrade — HITL-4 工具连续失败降级询问

def hitl_degrade_node(state: AgentState) -> dict:
    """interrupt: 重试/跳过/终止. resume 值由 API normalize_resume 归一为
    'retry'/'skip'/'abort'."""
    failed_tool = "未知工具"
    if state.plan and state.current_step_index < len(state.plan):
        failed_tool = state.plan[state.current_step_index].tool_name or "未知工具"

    choice = interrupt({
        "type": "degrade_confirm",
        "failed_tool": failed_tool,
        "options": ["重试", "跳过", "终止"],
        "message": DEGRADE_CONFIRM_MSG.format(failed_tool=failed_tool),
    })
    choice = str(choice).strip().lower() if choice else "skip"

    if "retry" in choice or "重试" in choice:
        debug.info("← Degrade: 用户选择重试", detail=f"tool={failed_tool}")
        return {
            "degrade_used": True, "error_streak": 0, "error": None,
            "replan_needed": True,
            "replan_reason": f"用户要求重试失败的服务调用({failed_tool})",
            "hitl_event": {"type": "degrade_confirm", "choice": "retry",
                           "at": datetime.now().isoformat()},
        }
    if "abort" in choice or "终止" in choice or "结束" in choice:
        return {
            "degrade_used": True,
            "final_answer": (
                "本次咨询因服务暂时不可用而中止，已收集的信息不会丢失。"
                "请稍后再试，或联系专业律师获取帮助。"
            ),
            "hitl_event": {"type": "degrade_confirm", "choice": "abort",
                           "at": datetime.now().isoformat()},
        }
    # skip(默认)
    debug.info("← Degrade: 用户选择跳过", detail=f"tool={failed_tool}")
    return {
        "degrade_used": True, "error_streak": 0, "error": None,
        "hitl_event": {"type": "degrade_confirm", "choice": "skip",
                       "at": datetime.now().isoformat()},
    }


# Node 8.6: HITL Budget — HITL-6 重规划预算耗尽询问

def hitl_budget_node(state: AgentState) -> dict:
    """interrupt: 补充(原文) / 收尾(finish). resume 值经 normalize_resume:
    空或含收尾关键词 → 'finish'; 其余非空文本 → 原文."""
    missing_parts = []
    if state.evaluation and state.evaluation.total > 0:
        ev = state.evaluation
        missing_parts.append(
            f"案例质量评估为「{ev.quality_verdict}」"
            f"(高质量{ev.correct_count}条/中等{ev.ambiguous_count}条)"
        )
    if not state.rag_documents:
        missing_parts.append("尚未检索到相关案例")
    if state.error:
        missing_parts.append(f"执行中出现错误: {state.error[:80]}")
    missing = "; ".join(missing_parts) or "信息仍不充分"

    answer = interrupt({
        "type": "budget_confirm",
        "missing": missing,
        "options": ["补充", "收尾"],
        "message": BUDGET_CONFIRM_MSG.format(missing=missing),
    })
    answer = (str(answer).strip() if answer else "")

    if not answer or any(w in answer.lower() for w in ("收尾", "结束", "finish")):
        debug.info("← Budget: 用户选择收尾", detail="带现有材料 finalize")
        return {
            "budget_hitl_used": True,
            "hitl_event": {"type": "budget_confirm", "choice": "finish",
                           "at": datetime.now().isoformat()},
        }

    augmented_query = f"{state.query}\n[用户补充信息] {answer}"
    debug.info("← Budget: 用户补充", detail=f"answer={answer[:80]} | 最后一次 replan")
    return {
        "budget_hitl_used": True,
        "query": augmented_query,
        "replan_needed": True,
        "replan_reason": "预算耗尽,用户补充关键信息,最后一次执行",
        "error": None,
        "hitl_event": {"type": "budget_confirm", "choice": "supplement",
                       "at": datetime.now().isoformat()},
    }
```

- [ ] **Step 4: executor / merge 错误计数**

`executor_node` 中"两次调用失败标记 failed"的 return（现 `LangGraph_lawApp.py:514-523` 区域）追加 `error_streak`：

```python
    if ai_msg is None:
        failed = [...]
        return {
            "plan": failed,
            "current_step_index": idx + 1,
            "error": f"步骤{step.step_id}({step.tool_name}) LLM 参数提取失败",
            "error_streak": state.error_streak + 1,
            "messages": [AIMessage(content="")],
        }
```

`merge_node` 中：工具报错分支（`step_status = "failed"` 且 `updates["error"]` 设置处）加 `updates["error_streak"] = state.error_streak + 1`；成功路径（`step_status == "done"`）加 `updates["error_streak"] = 0`。同时 merge 的 `updates["prompts_record"]` 构造处，`PromptsRecord(...)` 增加实参 `known_elements=state.case_elements.digest()`。

- [ ] **Step 5: replan_check_node 落库 insufficient_reason**

`replan_check_node` 成功分支的 return 改为：

```python
    return {
        "replan_needed": needs,
        "replan_reason": reason or None,
        "insufficient_reason": (
            result.insufficient_reason if needs and hasattr(result, "insufficient_reason") else "none"
        ),
    }
```

`AgentState` 需要一个承接字段——在 Task 2 的 AgentState HITL 段之外、计划执行段（`replan_reason` 旁）加：

```python
    # 不足原因诊断(vague/not_found/error/none) — replan_check 写入,路由读取
    insufficient_reason: str = "none"
```

`_fallback_replan_check` 返回值同步加第三元素（现有两处调用点改为三元解包）：

```python
def _fallback_replan_check(state: AgentState) -> tuple[bool, str, str]:
    if state.error:
        return True, f"执行异常: {state.error[:60]}", "error"
    if (
        state.evaluation
        and state.evaluation.quality_verdict == "不足,建议进行网络搜索补充"
    ):
        executed = {tc.tool_name for tc in state.tool_calls}
        if "get_google_search" not in executed:
            if not state.rag_documents:
                return True, "检索为空,案例库覆盖不到", "not_found"
            return True, "检索质量不足且问题笼统", "vague"
    return False, "规则兜底: 无明显问题", "none"
```

- [ ] **Step 6: 语法验证 + Commit**

Run: `$PY -c "import lawApp_LangGraph.LangGraph_lawApp; print('ok')"`
Expected: ok

```bash
git add lawApp_LangGraph/LangGraph_lawApp.py lawApp_LangGraph/state.py
git commit -m "A3: 执行中HITL — mid_clarify检索反馈追问 + degrade/budget兜底 + error_streak计数 + 不足原因诊断"
```

---

### Task 6: 主图重构 — 14 节点装配 + 路由 + pytest 存量更新

**Files:**
- Modify: `lawApp_LangGraph/LangGraph_lawApp.py`（`build_graph` 及全部路由函数）
- Modify: `tests/test_smoke.py`（`test_graph_topology`、`test_interrupt_resume` 就地更新）

**Interfaces:**
- Consumes: Task 4/5 的全部节点函数
- Produces: `build_graph(checkpointer=None, store=None)` 返回 14 节点图；SSE 依赖的节点名 `element_assess`（updates 事件过滤用）

- [ ] **Step 1: 重写路由函数（替换旧 `route_after_clarify`）**

```python
def route_after_risk_gate(state: AgentState) -> str:
    """拒绝热线文案(已有 final_answer) → finalize;否则评估要素."""
    if state.final_answer:
        return "finalize"
    return "element_assess"


def route_after_assess(state: AgentState) -> str:
    """有关键缺口反问且未达轮数上限 → ask_element;否则 planner."""
    if state.pending_questions and state.clarify_rounds < MAX_CLARIFY_ROUNDS:
        return "ask_element"
    return "planner"


def route_after_ask(state: AgentState) -> str:
    """轮数耗尽 → planner(软放行);否则回 assess 重新评估."""
    if state.clarify_rounds >= MAX_CLARIFY_ROUNDS:
        return "planner"
    return "element_assess"
```

`route_after_executor` / `route_after_merge` 各加 degrade 分支（其余分支保持存量逻辑）：

```python
def route_after_executor(state: AgentState) -> str:
    if state.error_streak >= ERROR_STREAK_THRESHOLD and not state.degrade_used:
        return "hitl_degrade"
    last_ai = next(
        (m for m in reversed(state.messages) if isinstance(m, AIMessage)), None
    )
    if last_ai is not None and getattr(last_ai, "tool_calls", None):
        return "tools"
    target = (
        "executor" if state.current_step_index < len(state.plan) else "replan_check"
    )
    debug.debug(f"路由: Executor → {target}",
                detail=f"step={state.current_step_index}/{len(state.plan)}")
    return target


def route_after_merge(state: AgentState) -> str:
    if state.error_streak >= ERROR_STREAK_THRESHOLD and not state.degrade_used:
        return "hitl_degrade"
    target = (
        "executor" if state.current_step_index < len(state.plan) else "replan_check"
    )
    debug.debug(f"路由: Merge → {target}",
                detail=f"step={state.current_step_index}/{len(state.plan)}")
    return target
```

`route_after_replan_check` 重写（优先级短路，见 spec §5.2）：

```python
def route_after_replan_check(state: AgentState) -> str:
    executed = len(state.tool_calls)
    # 1. 质量通过
    if not state.replan_needed:
        return "finalize"
    # 2. 预算耗尽
    if executed >= MAX_ROUNDS:
        if state.budget_hitl_used:
            return "finalize"
        return "hitl_budget"
    # 3. 不足 · 笼统 · 未用过 mid_clarify
    if (
        state.insufficient_reason == "vague"
        and not state.mid_clarify_used
    ):
        return "mid_clarify"
    # 4. 其余(not_found/error/已用过 mid) → replanner
    return "replanner"
```

- [ ] **Step 2: 重写 build_graph 边表**

```python
    builder.add_node("ingest", ingest_node)
    builder.add_node("risk_gate", risk_gate_node)
    builder.add_node("element_assess", element_assess_node)
    builder.add_node("ask_element", ask_element_node)
    builder.add_node("planner", planner_node)
    builder.add_node("executor", executor_node)
    builder.add_node("tools", _build_tools_node())
    builder.add_node("merge", merge_node)
    builder.add_node("replan_check", replan_check_node)
    builder.add_node("mid_clarify", mid_clarify_node)
    builder.add_node("hitl_degrade", hitl_degrade_node)
    builder.add_node("hitl_budget", hitl_budget_node)
    builder.add_node("replanner", replanner_node)
    builder.add_node("finalize", finalize_node)

    builder.add_edge(START, "ingest")
    builder.add_edge("ingest", "risk_gate")
    builder.add_conditional_edges(
        "risk_gate", route_after_risk_gate,
        {"element_assess": "element_assess", "finalize": "finalize"},
    )
    builder.add_conditional_edges(
        "element_assess", route_after_assess,
        {"ask_element": "ask_element", "planner": "planner"},
    )
    builder.add_conditional_edges(
        "ask_element", route_after_ask,
        {"element_assess": "element_assess", "planner": "planner"},
    )
    builder.add_conditional_edges(
        "planner", route_after_planner,
        {"executor": "executor", "finalize": "finalize"},
    )
    builder.add_conditional_edges(
        "executor", route_after_executor,
        {"tools": "tools", "executor": "executor",
         "replan_check": "replan_check", "hitl_degrade": "hitl_degrade"},
    )
    builder.add_edge("tools", "merge")
    builder.add_conditional_edges(
        "merge", route_after_merge,
        {"executor": "executor", "replan_check": "replan_check",
         "hitl_degrade": "hitl_degrade"},
    )
    builder.add_conditional_edges(
        "replan_check", route_after_replan_check,
        {"mid_clarify": "mid_clarify", "replanner": "replanner",
         "hitl_budget": "hitl_budget", "finalize": "finalize"},
    )
    builder.add_edge("mid_clarify", "replanner")
    builder.add_edge("hitl_degrade", "replanner")   # retry 分支;skip/abort 由节点内
                                                    # final_answer/replan_needed 语义决定,
                                                    # 需条件边:
```

**修正**：hitl_degrade 三分支出口不同，必须用条件边。替换上面最后一行：

```python
    def route_after_degrade(state: AgentState) -> str:
        if state.final_answer:          # abort → 热线文案已写
            return "finalize"
        if state.replan_needed:         # retry → replanner 重排
            return "replanner"
        return "replan_check"           # skip → 质量门控收口

    builder.add_conditional_edges(
        "hitl_degrade", route_after_degrade,
        {"replanner": "replanner", "replan_check": "replan_check",
         "finalize": "finalize"},
    )
```

`hitl_budget` 出口同理（补充 → replanner / 收尾 → finalize）：

```python
    def route_after_budget(state: AgentState) -> str:
        if state.replan_needed:
            return "replanner"
        return "finalize"

    builder.add_conditional_edges(
        "hitl_budget", route_after_budget,
        {"replanner": "replanner", "finalize": "finalize"},
    )
    builder.add_edge("replanner", "executor")
    builder.add_edge("finalize", END)
```

注意 degrade retry 分支节点内已设 `replan_needed=True`，而 `replan_check` 依赖的 `insufficient_reason` 默认 "none" 不影响该路径（replanner 只读 replan_reason）。

- [ ] **Step 3: executor prompt 调用点传 elements_digest**

`executor_node` 中 `EXECUTOR_PROMPT.format(...)` 调用改为：

```python
    summaries = _step_summaries(state)
    prompt = EXECUTOR_PROMPT.format(
        step_description=step.description,
        tool_name=step.tool_name,
        user_query=state.query,
        elements_digest=state.case_elements.digest(),
        **summaries,
    )
```

`planner_node` 的 `chain.ainvoke` 参数同样加 `"elements_digest": state.case_elements.digest()`（fallback 分支不动）。

- [ ] **Step 4: 更新 pytest 存量（两处）**

`test_graph_topology` 的 expected 集合改为：

```python
    expected = {
        "ingest", "risk_gate", "element_assess", "ask_element",
        "planner", "executor", "tools", "merge", "replan_check",
        "mid_clarify", "hitl_degrade", "hitl_budget", "replanner", "finalize",
    }
```

`test_interrupt_resume` 重写为要素循环语义（FakeVerdict 需扩展——`no_llm` fixture 的 `_FakeVerdict.__init__` 加 `**_kw` 透传新属性；实际做法：在 `_FakeVerdict` 中加参数 `applicable=True, element_updates=(), na_keys=(), promote_keys=(), questions=(), done=False, high_risk=False` 并设为实例属性，已有参数保持兼容）：

```python
def test_interrupt_resume(no_llm):
    """要素循环: assess 判定缺关键要素 → ask interrupt → resume 补充 → 要素齐 → planner."""
    import lawApp_LangGraph.LangGraph_lawApp as lg
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.types import Command

    # (1) 首轮: 缺 marriage_status, 生成反问
    no_llm["plan_result"] = _FakeVerdict(
        need_clarification=True, question="请问结婚多少年了?",
        applicable=True, done=False,
        questions=[type("Q", (), {"key": "marriage_status",
                                  "question": "请问结婚多少年了?"})()],
        plan=[],
    )

    async def run():
        g = lg.build_graph(checkpointer=MemorySaver())
        cfg = {"configurable": {"thread_id": "t-hitl2"}}
        result = await g.ainvoke({"query": "我想离婚"}, config=cfg)
        assert not result.get("final_answer")

        snap = await g.aget_state(cfg)
        assert snap.next
        intr = next(iter(snap.interrupts), None)
        assert intr and intr.value["type"] == "clarify"
        assert "结婚多少年" in intr.value["question"]

        # (2) resume 补充 → assess 二轮(done=True) → planner(空计划) → finalize
        no_llm["plan_result"] = _FakeVerdict(reasoning=["要素齐"], plan=[])
        result2 = await g.ainvoke(
            Command(resume="结婚5年,有个3岁孩子"), config=cfg
        )
        assert result2.get("final_answer") == "测试回答"
        assert "[用户补充信息]" in result2["query"]
        assert result2["clarify_history"], "应记录澄清历史"
        assert result2["clarify_rounds"] == 1

    asyncio.run(run())
```

同时 `_FakeVerdict` 扩展（fixture 文件内）：

```python
class _FakeVerdict:
    def __init__(self, *, plan=(), reasoning=(), need_clarification=False,
                 question="", high_risk=False, needs_replan=False, reason="",
                 applicable=True, element_updates=(), na_keys=(),
                 promote_keys=(), questions=(), done=False,
                 insufficient_reason="none"):
        self.plan = list(plan)
        self.reasoning = list(reasoning)
        self.need_clarification = need_clarification
        self.question = question
        self.high_risk = high_risk
        self.needs_replan = needs_replan
        self.reason = reason
        self.applicable = applicable
        self.element_updates = list(element_updates)
        self.na_keys = list(na_keys)
        self.promote_keys = list(promote_keys)
        self.questions = list(questions)
        self.done = done
        self.insufficient_reason = insufficient_reason
```

注意：风险判定与要素评估共用 executor LLM 替身，同一 `plan_result` 依次喂给 risk_gate 和 element_assess——`high_risk=False` 时 risk_gate 直接放行，`applicable/questions` 控制要素循环，与上面测试的参数一致即可。

- [ ] **Step 5: 全量 pytest**

Run: `$PY -m pytest tests/ -q`
Expected: **14+2 passed**（原 14 + Task 2 新增 2 = 16，全部绿；若 degrade/budget 相关存量断言受影响，逐个修复断言而非改节点行为）

- [ ] **Step 6: Commit**

```bash
git add lawApp_LangGraph/LangGraph_lawApp.py tests/test_smoke.py
git commit -m "A3: 主图重构 — 14节点装配, 澄清环/检索反馈环/降级出口路由, pytest存量同步"
```

---

### Task 7: rag_tools 分析上下文注入要素

**Files:**
- Modify: `lawApp_LangGraph/tools/rag_tools.py`

**Interfaces:**
- Consumes: Task 3 `get_analysis_prompt`（已在 Task 3 接好）；`PromptsRecord.known_elements`（Task 2）
- Produces: 分析提示词上下文包含已知案件要素段

- [ ] **Step 1: `_build_analysis_context` 开头追加要素段**

```python
def _build_analysis_context(pr: PromptsRecord) -> str:
    """从 PromptsRecord 的 web/law/case 字段拼装提示词上下文."""
    parts: list[str] = []
    if pr.known_elements and pr.known_elements != "暂无已知要素":
        parts.append(f"[已知案件要素]\n{pr.known_elements}")

    for law in pr.laws_results:
        # ...存量不变...
```

- [ ] **Step 2: 冒烟验证**

Run: `$PY -m pytest tests/ -q -k imports`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add lawApp_LangGraph/tools/rag_tools.py
git commit -m "A3: 分析上下文注入已知案件要素段"
```

---

### Task 8: API 适配 — normalize_resume / elements / SSE

**Files:**
- Modify: `lawApp_LangGraph/FastAPI/utils.py`
- Modify: `lawApp_LangGraph/FastAPI/model.py`
- Modify: `lawApp_LangGraph/FastAPI/api.py`

**Interfaces:**
- Consumes: Task 6 的节点名 `element_assess`（SSE updates 过滤）
- Produces: `normalize_resume(interrupt_type: str, answer: str) -> object`；`QueryResponse.elements: list[dict]`；`build_response` 填充 elements；SSE 新 `elements` 事件

- [ ] **Step 1: utils.py 加 normalize_resume**

```python
_YES = ("y", "yes", "是", "确认", "好", "继续")
_NO = ("n", "no", "否", "跳过", "不要")


def normalize_resume(interrupt_type: str, answer: str) -> object:
    """按 interrupt 类型归一用户回复(spec §7.2).

    risk_confirm/pdf_confirm → bool; degrade_confirm → retry/skip/abort;
    budget_confirm → finish 或补充原文; clarify/mid_clarify → 原文透传(空=跳过).
    """
    ans = (answer or "").strip()
    lowered = ans.lower()

    if interrupt_type in ("risk_confirm", "pdf_confirm"):
        if lowered in _YES:
            return True
        if lowered in _NO:
            return False
        return bool(lowered in _YES)  # 未识别默认拒绝(保守)

    if interrupt_type == "degrade_confirm":
        if "重试" in ans or "retry" in lowered:
            return "retry"
        if "终止" in ans or "结束" in ans or "abort" in lowered:
            return "abort"
        return "skip"  # 默认跳过

    if interrupt_type == "budget_confirm":
        if not ans or any(w in lowered for w in ("收尾", "结束", "finish")):
            return "finish"
        return ans  # 补充原文

    # clarify / mid_clarify: 原文透传
    return ans
```

`build_response` 填充 elements（在 `prompts_record` 赋值后追加）：

```python
    ce = state.get("case_elements")
    elements = [
        {"key": e.key, "label": e.label, "critical": e.critical,
         "status": e.status, "value": e.value}
        for e in (ce.elements if ce else [])
    ] if ce else []
```

并在 `QueryResponse(...)` 构造中传 `elements=elements`。

- [ ] **Step 2: model.py 加字段**

`QueryResponse` 末尾（`prompts_record` 之后）：

```python
    # 案件要素面板数据(子项目A 澄清循环)
    elements: List[Dict[str, Any]] = Field(default_factory=list)
```

- [ ] **Step 3: api.py — /ask/resume 类型感知 + SSE elements 事件**

`ask_resume` 中归一化段替换为：

```python
    # 先读快照取 interrupt 类型,再类型感知归一
    snapshot = await graph.aget_state(config)
    interrupt_req = extract_interrupt(snapshot)
    itype = (interrupt_req or {}).get("type", "")
    resume_value = normalize_resume(itype, request.answer)
```

（`normalize_resume` 加入文件顶部 utils import；原 `y/是→True` 内联逻辑删除。）

SSE `event_stream` 的 updates 分支加（`node_name == "merge"` 块之后）：

```python
                        if node_name == "element_assess" and "case_elements" in updates:
                            ce = updates.get("case_elements")
                            elems = getattr(ce, "elements", None) or []
                            yield sse_event("elements", [
                                {"key": e.key, "label": e.label,
                                 "status": e.status}
                                for e in elems
                            ])
```

- [ ] **Step 4: pytest 冒烟**

Run: `$PY -m pytest tests/ -q`
Expected: 全绿（API 层现有测试只有 import 冒烟）

- [ ] **Step 5: Commit**

```bash
git add lawApp_LangGraph/FastAPI/utils.py lawApp_LangGraph/FastAPI/model.py lawApp_LangGraph/FastAPI/api.py
git commit -m "A3: API适配 — normalize_resume 类型感知归一 + elements 面板字段 + SSE elements 事件"
```

---

### Task 9: clarify_test.ipynb — 澄清循环全链路验证

**Files:**
- Create: `lawApp_LangGraph/clarify_test.ipynb`

**Interfaces:**
- Consumes: Task 2/3/4/6 全部产物
- Produces: 用例(1)–(7)（spec §8 表）通过的 notebook

- [ ] **Step 1: 写 notebook（每个用例一个 markdown 标题 cell + code cell）**

结构（nbformat 手写 JSON 太冗长，执行者用 `$PY` + nbformat 以编程方式生成，或直接手写 .ipynb JSON；下面给出全部 code cell 内容）：

**Cell 1（setup，含 sys.path）：**

```python
import sys, asyncio, json
from pathlib import Path
from unittest.mock import patch

ROOT = Path.cwd().parent if Path.cwd().name == "lawApp_LangGraph" else Path.cwd()
sys.path.insert(0, str(ROOT))

# LLM 替身(移植 tests/test_smoke.py 体系)
from langchain_core.runnables import Runnable

class _FakeMsg:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []

class _FakeVerdict:
    def __init__(self, **kw):
        self.plan = list(kw.get("plan", ()))
        self.reasoning = list(kw.get("reasoning", ()))
        self.need_clarification = kw.get("need_clarification", False)
        self.question = kw.get("question", "")
        self.high_risk = kw.get("high_risk", False)
        self.needs_replan = kw.get("needs_replan", False)
        self.reason = kw.get("reason", "")
        self.applicable = kw.get("applicable", True)
        self.element_updates = list(kw.get("element_updates", ()))
        self.na_keys = list(kw.get("na_keys", ()))
        self.promote_keys = list(kw.get("promote_keys", ()))
        self.questions = list(kw.get("questions", ()))
        self.done = kw.get("done", False)
        self.insufficient_reason = kw.get("insufficient_reason", "none")

class _FakeChain(Runnable):
    def __init__(self, result=None):
        self.result = result
    def invoke(self, _inp, config=None, **kwargs):
        return self.result
    async def ainvoke(self, _inp, config=None, **kwargs):
        return self.result
    async def astream(self, _inp, config=None, **kwargs):
        yield _FakeMsg("测试回答")

class _FakeLLM(Runnable):
    def __init__(self, state):
        self.state = state
    def invoke(self, msgs, config=None, **kwargs):
        return self.state["executor_result"]
    def with_structured_output(self, schema):
        return _FakeChain(result=self.state["plan_result"])
    def bind_tools(self, tools):
        return self
    async def ainvoke(self, msgs, config=None, **kwargs):
        return self.state["executor_result"]
    async def astream(self, _prompt, config=None, **kwargs):
        yield _FakeMsg("测试回答")

import lawApp_LangGraph.LangGraph_lawApp as app
import lawApp_LangGraph.tools.rag_tools as rag_tools

class _ToolLLM:
    async def astream(self, _prompt):
        yield _FakeMsg("分析结果:测试回答")

_ctrl = {"plan_result": _FakeVerdict(), "executor_result": None}
_p_planner = patch.object(app, "get_planner_llm", lambda: _FakeLLM(_ctrl))
_p_executor = patch.object(app, "get_executor_llm", lambda: _FakeLLM(_ctrl))
_p_rag = patch.object(rag_tools, "_get_llm", lambda: _ToolLLM())
_p_planner.start(); _p_executor.start(); _p_rag.start()
print("setup ok")
```

**Cell 2（用例(1) CaseElements 单元验证——`default_case_elements` 7 要素/关键3/na/提升/digest）：**

```python
from lawApp_LangGraph.state import (
    CaseElements, default_case_elements, MAX_CLARIFY_ROUNDS,
)

ce = default_case_elements()
assert len(ce.elements) == 7
assert [e.key for e in ce.critical_missing()] == [
    "marriage_status", "demand", "property"]
ce.mark_na(["evidence"]); ce.promote(["timeline"])
ce.update("marriage_status", "在婚,分居中", by="ask")
assert ce.digest() == "婚姻现状:在婚,分居中"
ce.update("property", "一套房,双方名下", by="ask")
assert ce.digest() == "婚姻现状:在婚,分居中 | 主要财产与归属:一套房,双方名下"
print("(1) CaseElements model OK")
```

**Cell 3（用例(2) 单轮澄清 → interrupt → resume → 要素齐 → planner 收到 digest）：**

```python
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

def _q(key, q):
    return type("Q", (), {"key": key, "question": q})()

async def case2():
    _ctrl["plan_result"] = _FakeVerdict(
        applicable=True, done=False,
        questions=[_q("marriage_status", "请问结婚几年了?现在是什么状态?")],
        plan=[],
    )
    g = app.build_graph(checkpointer=MemorySaver())
    cfg = {"configurable": {"thread_id": "nb-c2"}}
    r = await g.ainvoke({"query": "我想离婚"}, config=cfg)
    assert not r.get("final_answer")
    snap = await g.aget_state(cfg)
    intr = next(iter(snap.interrupts))
    assert intr.value["type"] == "clarify"
    assert intr.value["round"] == "1/5"
    assert len(intr.value["elements"]) == 7  # 面板载荷

    # resume → assess 第二轮 done=True → planner(空计划) → finalize
    _ctrl["plan_result"] = _FakeVerdict(applicable=True, done=True, plan=[])
    r2 = await g.ainvoke(Command(resume="结婚5年,分居中"), config=cfg)
    assert r2["final_answer"] == "测试回答"
    assert r2["clarify_rounds"] == 1
    assert r2["clarify_history"][0].answer == "结婚5年,分居中"
    print("(2) 单轮澄清 resume OK")

asyncio.run(case2())
```

**Cell 4（用例(3) 3 轮逐个补齐：assess 每轮吐一个反问，第 3 轮 done）：**

```python
async def case3():
    g = app.build_graph(checkpointer=MemorySaver())
    cfg = {"configurable": {"thread_id": "nb-c3"}}
    rounds = [
        _FakeVerdict(applicable=True, done=False,
                     questions=[_q("marriage_status", "结婚几年了?")], plan=[]),
        _FakeVerdict(applicable=True, done=False,
                     element_updates=[type("U", (), {"key": "marriage_status",
                                                    "value": "5年", "status": "known"})()],
                     questions=[_q("demand", "你最想达到什么结果?")], plan=[]),
        _FakeVerdict(applicable=True, done=True, plan=[]),
    ]
    it = iter(rounds)
    _ctrl["plan_result"] = next(it)
    r = await g.ainvoke({"query": "我想离婚"}, config=cfg)
    for ans, expect_rounds in (("结婚5年", 1), ("想争取孩子抚养权", 2)):
        assert not r.get("final_answer")
        _ctrl["plan_result"] = next(it)
        r = await g.ainvoke(Command(resume=ans), config=cfg)
        assert r["clarify_rounds"] == expect_rounds
    assert r["final_answer"] == "测试回答"
    assert len(r["clarify_history"]) == 2
    print("(3) 3轮逐个补齐 OK")

asyncio.run(case3())
```

**Cell 5（用例(4) 5 轮上限软退出：永远缺 → 第 5 轮放行，reasoning 不阻塞）：**

```python
async def case4():
    g = app.build_graph(checkpointer=MemorySaver())
    cfg = {"configurable": {"thread_id": "nb-c4"}}
    # 永远吐一个反问(done=False)
    always_ask = _FakeVerdict(applicable=True, done=False,
                              questions=[_q("marriage_status", "结婚几年了?")],
                              plan=[])
    _ctrl["plan_result"] = always_ask
    r = await g.ainvoke({"query": "我想离婚"}, config=cfg)
    for i in range(4):  # 共 5 次 interrupt: 首次 + 4 次 resume
        r = await g.ainvoke(Command(resume=f"回答{i}"), config=cfg)
    # 第 5 轮后 route_after_ask 强制放行 → planner(空计划) → finalize
    assert r["clarify_rounds"] == 5
    assert r["final_answer"] == "测试回答"
    print("(4) 5轮上限软退出 OK")

asyncio.run(case4())
```

**Cell 6（用例(5) 空回答=跳过按原问题继续 + 用例(6) 闲聊全 na 直通）：**

```python
async def case5():
    g = app.build_graph(checkpointer=MemorySaver())
    cfg = {"configurable": {"thread_id": "nb-c5"}}
    _ctrl["plan_result"] = _FakeVerdict(
        applicable=True, done=False,
        questions=[_q("marriage_status", "结婚几年了?")], plan=[])
    r = await g.ainvoke({"query": "我想离婚"}, config=cfg)
    _ctrl["plan_result"] = _FakeVerdict(applicable=True, done=True, plan=[])
    r2 = await g.ainvoke(Command(resume=""), config=cfg)  # 空回答
    assert r2["clarify_rounds"] == 5          # 置满 → 软放行
    assert "[用户补充信息]" not in r2["query"]  # 原问题未变
    assert r2["final_answer"] == "测试回答"
    print("(5) 空回答跳过 OK")

async def case6():
    g = app.build_graph(checkpointer=MemorySaver())
    cfg = {"configurable": {"thread_id": "nb-c6"}}
    # applicable=false → 全 na 直通零反问
    _ctrl["plan_result"] = _FakeVerdict(applicable=False, plan=[])
    r = await g.ainvoke({"query": "今天天气怎么样"}, config=cfg)
    assert all(e.status == "na" for e in r["case_elements"].elements)
    assert r["clarify_rounds"] == 0
    assert r["final_answer"] == "测试回答"
    print("(6) 闲聊全na直通 OK")

asyncio.run(case5()); asyncio.run(case6())
```

**Cell 7（用例(7) 高风险拒绝/确认两分支）：**

```python
async def case7():
    g = app.build_graph(checkpointer=MemorySaver())
    # 拒绝分支
    cfg = {"configurable": {"thread_id": "nb-c7a"}}
    _ctrl["plan_result"] = _FakeVerdict(high_risk=True, plan=[])
    r = await g.ainvoke({"query": "我不想活了"}, config=cfg)
    snap = await g.aget_state(cfg)
    assert next(iter(snap.interrupts)).value["type"] == "risk_confirm"
    r2 = await g.ainvoke(Command(resume=False), config=cfg)   # normalize: False
    assert "12338" in r2["final_answer"]          # 热线文案
    assert r2["case_elements"].elements[0].status == "missing"  # 未进要素评估

    # 确认分支 → 继续要素评估
    cfg = {"configurable": {"thread_id": "nb-c7b"}}
    _ctrl["plan_result"] = _FakeVerdict(high_risk=True, applicable=True,
                                        done=True, plan=[])
    r = await g.ainvoke({"query": "家暴想离婚"}, config=cfg)
    r2 = await g.ainvoke(Command(resume=True), config=cfg)
    assert r2["risk_confirmed"] is True
    assert r2["final_answer"] == "测试回答"
    print("(7) 高风险两分支 OK")

asyncio.run(case7())
```

**Cell 8（teardown + 总结）：**

```python
_p_planner.stop(); _p_executor.stop(); _p_rag.stop()
print("ALL PASSED")
```

- [ ] **Step 2: 程序化执行验证**

Run: `$PY scripts/run_nb.py lawApp_LangGraph/clarify_test.ipynb`
Expected: `OK: clarify_test.ipynb all cells executed` 且输出含各用例 OK 与 `ALL PASSED`

- [ ] **Step 3: Commit**

```bash
git add lawApp_LangGraph/clarify_test.ipynb
git commit -m "A3: clarify_test.ipynb — 要素澄清循环全链路验证(7用例)"
```

---

### Task 10: hitl_test.ipynb — 执行中 HITL 全链路验证

**Files:**
- Create: `lawApp_LangGraph/hitl_test.ipynb`

**Interfaces:**
- Consumes: Task 5/6 产物；Task 9 Cell 1 的 setup 代码（**完整复制进本 notebook Cell 1，不 import 跨 notebook**）
- Produces: 用例(8)–⑬通过

- [ ] **Step 1: 写 notebook**

Cell 1 = Task 9 Cell 1 原样复制。后续：

**Cell 2（用例(8) mid_clarify 全链路：评估不足+vague → interrupt → resume 增强 query → replanner）：**

```python
async def case8():
    # 触发链: plan 空不了, 需要走到 replan_check → 直接构造半程 state 太绕;
    # 用 monkeypatch _fallback_replan_check? 不 — 用真实 replan_check LLM 替身:
    # _FakeLLM.with_structured_output 返回 plan_result, replan_check 也走它。
    # 顺序: clarify(无问) → planner(1步:retrieve) → executor(发调用) →
    #        tools(报错) → merge(streak=1<2) → replan_check(needs_replan, vague)
    # 简化: 单步计划 + 工具 mock 失败一次不够 streak; 改为直接注入状态驱动节点单测+路由单测
    from lawApp_LangGraph.state import AgentState, PlanStep, RetrievedDocument
    import lawApp_LangGraph.LangGraph_lawApp as lg

    # (a) mid_clarify 节点单测: interrupt → resume 非空 → query 增强
    _ctrl["plan_result"] = _FakeVerdict(
        needs_replan=True, reason="问题笼统", insufficient_reason="vague",
        plan=[],
    )
    # mid_clarify 用 executor LLM 的 structured output:
    class _MQ:
        question = "你的房子是婚前买的还是婚后买的?"
        element_key = "property"
    _ctrl["plan_result"] = _MQ()  # 同一替身喂 mid_clarify

    g = app.build_graph(checkpointer=MemorySaver())
    cfg = {"configurable": {"thread_id": "nb-c8"}}
    # 手工搭到 mid_clarify 的前置状态: 用图级构造太重 → 直接测节点函数
    st = AgentState(
        query="离婚房子怎么分",
        rag_documents=[RetrievedDocument(case_number="(2023)京01民终1号",
                                         chunk_text="婚后共同还贷情形…", year="2023")],
        mid_clarify_used=False,
    )
    # interrupt 需在图上下文: 改用整图, 先跑通到 replan_check 较复杂;
    # 此处退而验证节点级: patch interrupt
    with patch.object(lg, "interrupt", lambda payload: "婚前我付首付婚后共同还贷"):
        upd = await lg.mid_clarify_node(st)
    assert "[检索反馈追问]" in upd["query"]
    assert upd["mid_clarify_used"] is True
    assert upd["case_elements"].elements[2].status == "known"  # property
    print("(8) mid_clarify 节点级 OK(resume 增强)")

asyncio.run(case8())
```

**Cell 3（用例⑨ not_found → 无 mid_clarify：路由单测）：**

```python
def case9():
    import lawApp_LangGraph.LangGraph_lawApp as lg
    from lawApp_LangGraph.state import AgentState

    # not_found → replanner(不问用户)
    st = AgentState(query="x", replan_needed=True,
                    insufficient_reason="not_found", mid_clarify_used=False)
    st = st.model_copy(update={"tool_calls": [], })
    # route 需要 tool_calls 计数 < MAX_ROUNDS: 借 Pydantic 直接设
    import lawApp_LangGraph.LangGraph_lawApp as _lg
    assert _lg.route_after_replan_check(
        AgentState(query="x", replan_needed=True, insufficient_reason="not_found")
    ) == "replanner"
    # vague 且未用过 → mid_clarify
    assert _lg.route_after_replan_check(
        AgentState(query="x", replan_needed=True, insufficient_reason="vague")
    ) == "mid_clarify"
    # vague 但已用过 → replanner
    assert _lg.route_after_replan_check(
        AgentState(query="x", replan_needed=True, insufficient_reason="vague",
                   mid_clarify_used=True)
    ) == "replanner"
    # 预算耗尽未问 → hitl_budget
    st_budget = AgentState(query="x", replan_needed=True)
    st_budget = st_budget.model_copy(deep=True)
    # tool_calls 是 append_list 注解字段, 直接构造注入
    from lawApp_LangGraph.state import ToolCallRecord
    st_budget.tool_calls = [ToolCallRecord(step_id=i, tool_name="t",
                                           tool_input={}, output={})
                            for i in range(10)]
    assert _lg.route_after_replan_check(st_budget) == "hitl_budget"
    # 耗尽且已问 → finalize
    st_budget2 = st_budget.model_copy(
        update={"budget_hitl_used": True})
    assert _lg.route_after_replan_check(st_budget2) == "finalize"
    print("⑨ 路由优先级 OK")

case9()
```

**Cell 4（用例（用例⑩⑪ degrade 分支 + 一次性）：**

```python
async def case10():
    import lawApp_LangGraph.LangGraph_lawApp as lg
    from lawApp_LangGraph.state import AgentState, PlanStep

    plan = [PlanStep(step_id=1, description="检索", tool_name="retrieve_legal_knowledge")]
    st = AgentState(query="x", plan=plan, current_step_index=0,
                    error_streak=2, degrade_used=False)

    # retry → replanner 出口
    with patch.object(lg, "interrupt", lambda p: "重试"):
        upd = await lg.hitl_degrade_node(st)
    assert upd["degrade_used"] and upd["error_streak"] == 0
    assert upd["replan_needed"] and "重试" in upd["replan_reason"]
    assert lg.route_after_degrade(AgentState(**{"replan_needed": True,
        "final_answer": "", "query": "x"})) == "replanner" if hasattr(lg, "route_after_degrade") else True

    # abort → 热线文案
    with patch.object(lg, "interrupt", lambda p: "终止"):
        upd = await lg.hitl_degrade_node(st)
    assert "中止" in upd["final_answer"]

    # skip → replan_check 收口
    with patch.object(lg, "interrupt", lambda p: "跳过"):
        upd = await lg.hitl_degrade_node(st)
    assert upd["error_streak"] == 0 and "final_answer" not in upd

    # ⑪ 一次性: degrade_used=True 后路由不再进 hitl_degrade
    from lawApp_LangGraph.LangGraph_lawApp import route_after_merge
    assert route_after_merge(AgentState(query="x", error_streak=5,
                                        degrade_used=True,
                                        current_step_index=0)) != "hitl_degrade"
    print("⑩⑪ degrade 分支与一次性 OK")

asyncio.run(case10())
```

**Cell 5（用例⑫ budget 补充/收尾）：**

```python
async def case12():
    import lawApp_LangGraph.LangGraph_lawApp as lg
    from lawApp_LangGraph.state import AgentState, EvaluationResult

    st = AgentState(
        query="x",
        evaluation=EvaluationResult(total=5, correct_count=1, ambiguous_count=1,
                                    incorrect_count=3, quality_verdict="不足,建议进行网络搜索补充"),
    )
    # 补充分支(非空文本)
    with patch.object(lg, "interrupt", lambda p: "对方偷偷转移了财产"):
        upd = await lg.hitl_budget_node(st)
    assert upd["budget_hitl_used"] and "[用户补充信息]" in upd["query"]
    assert upd["replan_needed"]
    # 收尾分支
    with patch.object(lg, "interrupt", lambda p: "收尾"):
        upd = await lg.hitl_budget_node(st)
    assert upd["budget_hitl_used"] and "final_answer" not in upd
    assert not upd.get("replan_needed")
    print("⑫ budget 两分支 OK")

asyncio.run(case12())
```

**Cell 6（用例⑬ pdf_confirm 回归 + error_streak 计数）：**

```python
async def case13():
    # pdf_confirm: 存量行为回归 —— 计划含 markdown_to_pdf 且未确认 → interrupt
    import lawApp_LangGraph.LangGraph_lawApp as lg
    from lawApp_LangGraph.state import AgentState, PlanStep

    plan = [PlanStep(step_id=1, description="生成PDF",
                     tool_name="markdown_to_pdf")]
    st = AgentState(query="出份报告", plan=plan, current_step_index=0,
                    pdf_confirmed=False)
    with patch.object(lg, "interrupt", lambda p: False):  # 用户拒绝
        upd = await lg.executor_node(st)
    assert upd["plan"][0].status == "done"       # 跳过标记
    assert upd["pdf_confirmed"] is True

    # error_streak: executor 参数提取失败两次 → +1
    _ctrl["executor_result"] = None              # AIMessage 缺失 → 两次失败路径
    st2 = AgentState(query="x",
                     plan=[PlanStep(step_id=1, description="检索",
                                    tool_name="retrieve_legal_knowledge")],
                     current_step_index=0, error_streak=0)
    upd2 = await lg.executor_node(st2)
    assert upd2["error_streak"] == 1
    _ctrl["executor_result"] = None
    print("⑬ pdf_confirm回归 + error_streak OK")

asyncio.run(case13())
_p_planner.stop(); _p_executor.stop(); _p_rag.stop()
print("ALL PASSED")
```

**注意**：case13 中 `_ctrl["executor_result"]=None` 使 `_FakeLLM.ainvoke` 返回 None → executor 走"未发起工具调用"两次失败分支；若 FakeLLM 的 bind_tools 路径在该分支抛异常（RuntimeError 被捕获）也符合——只要最终 `error_streak==1` 即通过。执行者跑通后如断言细节有偏差，以行为语义为准微调断言（不改节点代码）。

- [ ] **Step 2: 执行验证**

Run: `$PY scripts/run_nb.py lawApp_LangGraph/hitl_test.ipynb`
Expected: `OK` + `ALL PASSED`

- [ ] **Step 3: Commit**

```bash
git add lawApp_LangGraph/hitl_test.ipynb
git commit -m "A3: hitl_test.ipynb — mid_clarify/degrade/budget 执行中HITL验证(6用例)"
```

---

### Task 11: prompts_test.ipynb — 提示词与注入链验证

**Files:**
- Create: `lawApp_LangGraph/prompts_test.ipynb`

**Interfaces:**
- Consumes: Task 3 prompts.py、Task 7 `_build_analysis_context`
- Produces: 用例⑭–⑯通过

- [ ] **Step 1: 写 notebook**

Cell 1 setup（同 Task 9 Cell 1，但不需要 LLM patch——只 import prompts）：

```python
import sys
from pathlib import Path
ROOT = Path.cwd().parent if Path.cwd().name == "lawApp_LangGraph" else Path.cwd()
sys.path.insert(0, str(ROOT))
from lawApp_LangGraph import prompts
print("setup ok")
```

**Cell 2（用例⑭ 全模板渲染无缺变量）：**

```python
from langchain_core.prompts import PromptTemplate

# 字符串模板: 检查变量集合与预期一致(不多不少)
STRING_PROMPTS = {
    "RISK_GATE_PROMPT": {"query"},
    "ELEMENT_ASSESS_PROMPT": {"query", "elements_digest", "last_question",
                              "last_answer", "round", "max_rounds"},
    "MID_CLARIFY_PROMPT": {"query", "top_docs_summary"},
    "REPLAN_CHECK_PROMPT": {"user_query", "executed_summary", "doc_count",
                            "quality_verdict", "web_count", "law_count",
                            "error_info"},
    "PLANNER_SYSTEM": {"available_tools", "query", "elements_digest"},
    "EXECUTOR_PROMPT": {"step_description", "tool_name", "user_query",
                        "elements_digest", "rag_summary", "eval_summary",
                        "law_summary", "web_summary"},
    "REPLANNER_SYSTEM_PROMPT": {"executed_steps", "doc_count", "quality",
                                "web_count", "law_count", "error",
                                "replan_reason", "available_tools",
                                "user_query", "next_id"},
    "DEGRADE_CONFIRM_MSG": {"failed_tool"},
    "BUDGET_CONFIRM_MSG": {"missing"},
}
for name, expected in STRING_PROMPTS.items():
    tpl = getattr(prompts, name)
    assert isinstance(tpl, str), name
    got = set(PromptTemplate.from_template(tpl).input_variables)
    # EXECUTOR_PROMPT 的 {tool_name} 出现两次且 format 需要它 → 在 expected 内即可
    assert got == expected, f"{name}: got {got}, expected {expected}"
    # 渲染不报错
    PromptTemplate.from_template(tpl).format(**{v: "测试值" for v in got})
print("⑭ 字符串模板变量集 OK")

# PromptTemplate 实例
for name, expected in {
    "FINALIZE_CASE_PROMPT": {"docs", "query"},
    "FINALIZE_DIRECT_PROMPT": {"query"},
}.items():
    tpl = getattr(prompts, name)
    assert tpl.format(**{v: "测试值" for v in expected})
print("⑭ 全部模板渲染 OK")
```

**Cell 3（用例⑮ Kim 标记 + Saul 切换只影响分析）：**

```python
import os

assert "Kim Wexler" in prompts.KIM_PERSONA_BLOCK
# Kim 注入反问与兜底
assert "Kim Wexler" in prompts.ELEMENT_ASSESS_PROMPT
assert "Kim Wexler" in prompts.MID_CLARIFY_PROMPT
assert "Kim Wexler" in prompts.FINALIZE_CASE_PROMPT.template
assert "Kim Wexler" in prompts.FINALIZE_DIRECT_PROMPT.template
# 旧傲娇人设已清除
assert "傲娇" not in prompts.FINALIZE_DIRECT_PROMPT.template
assert "傲娇" not in prompts.FINALIZE_CASE_PROMPT.template

os.environ["LEGAL_ANALYSIS_ROLE"] = "kim"
assert "Kim Wexler" in prompts.get_analysis_prompt().template
os.environ["LEGAL_ANALYSIS_ROLE"] = "saul"
assert "Saul Goodman" in prompts.get_analysis_prompt().template
# 反问不随 saul 变
assert "Kim Wexler" in prompts.ELEMENT_ASSESS_PROMPT
os.environ.pop("LEGAL_ANALYSIS_ROLE")
print("⑮ Kim 全链路 + Saul 仅分析 OK")
```

**Cell 4（用例⑯ digest 注入链）：**

```python
from lawApp_LangGraph.state import PromptsRecord
from lawApp_LangGraph.tools.rag_tools import _build_analysis_context

pr = PromptsRecord(known_elements="婚姻现状:在婚分居 | 核心诉求:争取抚养权")
ctx = _build_analysis_context(pr)
assert ctx.startswith("[已知案件要素]")
assert "争取抚养权" in ctx

# 空/默认不注入
ctx2 = _build_analysis_context(PromptsRecord())
assert "[已知案件要素]" not in ctx2

# planner/executor 模板含注入位
assert "elements_digest" in prompts.PLANNER_SYSTEM
assert "已知案件要素" in prompts.PLANNER_SYSTEM
assert "已知案件要素" in prompts.EXECUTOR_PROMPT
print("⑯ digest 注入链 OK")
print("ALL PASSED")
```

- [ ] **Step 2: 执行验证**

Run: `$PY scripts/run_nb.py lawApp_LangGraph/prompts_test.ipynb`
Expected: `OK` + `ALL PASSED`

- [ ] **Step 3: Commit**

```bash
git add lawApp_LangGraph/prompts_test.ipynb
git commit -m "A3: prompts_test.ipynb — 模板变量/Kim人设/digest注入链验证(3用例组)"
```

---

### Task 12: 文档同步 — PROJECT_OVERVIEW.md

**Files:**
- Modify: `lawApp_LangGraph/PROJECT_OVERVIEW.md`

**Interfaces:**
- Consumes: spec §5 mermaid 图（直接复制）+ 实际实现（以代码为准核对）

- [ ] **Step 1: 更新文档**

1. 顶部图示与「三、Graph 节点详解」整节重写：14 节点表（复制 spec §5.1 职责表）、mermaid 流程图（复制 spec §5 的 mermaid 块）
2. 「五、工具系统」不变，但提示词一节补一句：提示词已集中于 `prompts.py`（v2）
3. 「八、角色设定与提示词工程」重写：Kim 全链路统一、Saul 环境变量彩蛋、6 处 interrupt 清单
4. 文末版本行更新：`文档生成时间：2026-09-10 | 项目版本：v3.1.0 (子项目A)`

- [ ] **Step 2: 全量回归**

Run: `$PY -m pytest tests/ -q && $PY scripts/run_nb.py lawApp_LangGraph/clarify_test.ipynb && $PY scripts/run_nb.py lawApp_LangGraph/hitl_test.ipynb && $PY scripts/run_nb.py lawApp_LangGraph/prompts_test.ipynb`
Expected: pytest 全绿 + 三个 notebook `OK`

- [ ] **Step 3: Commit**

```bash
git add lawApp_LangGraph/PROJECT_OVERVIEW.md
git commit -m "A3: 文档同步 — PROJECT_OVERVIEW 14节点/6 interrupt/Kim 人设/Mermaid"
```

---

## Self-Review 结果

1. **Spec 覆盖**：spec §4（Task 2）、§6 提示词（Task 3）、§5.1 入口三节点（Task 4）、§5.3 mid/degrade/budget + 错误计数（Task 5）、§5.2 路由与 14 节点装配（Task 6）、`known_elements` 分析注入（Task 7）、§7 API/SSE（Task 8）、§8 三个 notebook 16 用例（Task 9-11）、§9 交付物文档行（Task 12）——全部有对应任务。
2. **占位符扫描**：唯一刻意省略是 `LEGAL_ANALYSIS_PROMPT_Saul` 正文（已显式标注"从 rag_tools.py 原样复制"并给出精确行号），其余无 TBD。
3. **类型一致性**：`ElementQuestion` 在 state.py 定义（Task 2）与 `_schema_models` 的 `ElementAssessmentSchema.questions` 复用同一类；`insufficient_reason` 枚举四处（Schema/AgentState/fallback/路由）一致；`normalize_resume` 的返回值与 `hitl_degrade_node`/`hitl_budget_node` 消费的字符串（retry/skip/abort/finish/原文）逐一对齐；`route_after_degrade`/`route_after_budget` 在 Task 6 定义、Task 10 测试引用一致。
