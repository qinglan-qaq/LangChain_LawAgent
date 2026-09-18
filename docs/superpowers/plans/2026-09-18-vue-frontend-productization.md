# 子项目C「Vue 前端产品化 + 双模式咨询」Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把现有 LangGraph 法律咨询 Agent 产品化为 Vue 前端应用：律师助理模式（婚姻家事起诉状/答辩状起草）+ 代理律师模式（多轮追问咨询），前端实时展示 reasoner 原生 CoT 思考流。

**Architecture:** 图结构零改动——mode/doc_type 经 graph input 注入 state，节点按模式选提示词变体；reasoner 的 reasoning_content 经进程内 asyncio.Queue 总线（按 thread_id 键控）旁路推给 SSE 端点；前端单一界面按钮切换两模式。新代码路径不做兜底，有错误直接报错（用户决策）。

**Tech Stack:** Python 3.11 / FastAPI / LangGraph 1.0.1 / DeepSeek（planner=deepseek-reasoner, executor=deepseek-chat）/ Vue 3.5 + Vite 8 + Tailwind CSS v4 + Inspira UI（复制安装）+ motion-v + lucide-vue-next + reka-ui + axios（REST）/ fetch（SSE 流读）

**Spec:** `docs/superpowers/specs/2026-09-18-vue-frontend-productization-design.md`（commit 871f136，含 8 条用户决策修订）

## Global Constraints

- 解释器一律 `F:/Anaconda_env/lawApp_langGraph/python.exe`（下称 `$PY`）；前端命令在 `frontend/` 下 `npm`
- **不做兜底（用户决策）**：Task 4 起的新代码路径错误直接抛出/返回错误；存量 pytest 与旧 `/ask` 端点不改动、不新增替身用例
- **测试用 ipynb 形式（用户决策）**：新测试全部进 `tests_ipynb/`（复用 nbkit 三态收集），直连真实后端 + 真实 LLM，**不用 FakeLLM**
- 六个 interrupt 类型标签字符串精确不变：`risk_confirm` / `clarify` / `pdf_confirm` / `degrade_confirm` / `mid_clarify` / `budget_confirm`
- 双模式 = 两个独立 FastAPI 端点（`/attorney/*`、`/assistant/*`），mode 不作为请求字段；旧 `/ask`、`/ask/stream` 保留标 deprecated
- 免责声明非阻塞小弹窗（用户决策）：后端不做 403 拦截，文本经 `GET /disclaimer` 提供
- 前端单界面复用（用户决策）：按钮切换模式，仅输入区配置变化；HITL 面板各类型均附自由文本「自助输入」
- REST 用 axios；SSE 流式读用 fetch + ReadableStream（axios/XHR 不支持流读，spec 已注明）
- 文书业务范围仅婚姻家事类：`doc_type ∈ {"complaint"=起诉状, "defense"=答辩状}`
- 每任务结束 `git commit`，message 中文、前缀 `C:`
- 新增/改写函数 docstring 用 Google 风格；节点/工具内不画 `# ----` 分隔线

## 关键事实速查（已核实，写码时直接引用）

- 结构化输出调用点：`LangGraph_lawApp.py:284`(RiskSchema)/`:358`(ElementAssessmentSchema)/`:532`(planner)/`:911`(ReplanCheckSchema)/`:1001`(MidClarifySchema)/`:1077`(replanner)
- Schema 字段（`LangGraph_lawApp.py:151-202` + `state.py:139-152`）：`PlanSchema{reasoning:str[], plan:[{step_id,description,tool_name}]}`、`RiskSchema{high_risk:bool, reason:str}`、`ElementAssessmentSchema{applicable:bool, element_updates:[{key,value,status}], na_keys:str[], promote_keys:str[], questions:[{key,question}], done:bool}`、`ReplanCheckSchema{needs_replan:bool, reason:str, insufficient_reason∈{vague,not_found,error,none}}`、`MidClarifySchema{question:str, element_key:str}`
- `CaseElement{key,label,critical,status∈{known,missing,na},value,updated_by}`；`CaseElements.elements` + `digest()`
- 提示词常量（prompts.py）：`RISK_GATE_PROMPT:32` `ELEMENT_ASSESS_PROMPT:49` `MID_CLARIFY_PROMPT:85` `REPLAN_CHECK_PROMPT:107` `PLANNER_SYSTEM:134` `REPLANNER_SYSTEM_PROMPT:180` `FINALIZE_CASE_PROMPT:213`
- api.py：`/ask:150` `/ask/resume:178` `/ask/stream:210`（astream `["updates","messages","values"]`）；utils：`ensure_session/get_graph/graph_config/extract_interrupt/normalize_resume/build_response/sse_event`；`graph_config(sid)` 的 configurable.thread_id = sid
- SSE 帧格式（utils.py:159）：`data: {"event": E, "data": D}\n\n` 单行 JSON 信封
- law_cases DDL（db.py:87-95）：`id TEXT PK, year TEXT, case_number TEXT, case_cause TEXT, chunk_index INT, chunk_text TEXT, embedding VECTOR(1024)`；`db.upsert_session(session_id, user_id, meta, last_active_at)`、`record_audit`、`get_pool/close_pool` 已存在
- 案例语料：`data/Documents/MarkDownFiles/*.md`（11 份，形如 `中国法院2014年度案例_婚姻家庭与继承纠纷.md`，正文首行 `# 案件标题`）
- 前端骨架：`lawApp_LangGraph/law_agent_Vue/`（59 行 App.vue，vite proxy `/api→127.0.0.1:8000` 已配好）
- planner 现签名 `async def planner_node(state: AgentState) -> dict`（`:509`）；`_normalize_plan(result)` 已存在

---

### Task 1: 四节点 json_mode 修复（P0.1）

**Files:**
- Modify: `lawApp_LangGraph/prompts.py:32/49/85/107`
- Modify: `lawApp_LangGraph/LangGraph_lawApp.py:284/358/911/1001`

**Interfaces:**
- Produces: 四处 `with_structured_output(Schema, method="json_mode")`；四个提示词尾部带字段声明（Task 3 的变体提示词以这些声明为基准拼接）
- 说明: planner/replanner（532/1077）**本任务不动**，Task 4 一并改手工流式

- [ ] **Step 1: 四个提示词尾部追加字段声明**（json_mode 要求字段名在提示词里显式声明，02 册实测结论）

`prompts.py:32` `RISK_GATE_PROMPT` 字符串末尾追加：
```
只输出一个 JSON 对象,字段名必须与下面完全一致(不要输出任何其他文本):
{"high_risk": true 或 false, "reason": "命中的标准,不超过30字"}
```

`prompts.py:49` `ELEMENT_ASSESS_PROMPT` 末尾追加：
```
只输出一个 JSON 对象,字段名必须与下面完全一致(不要输出任何其他文本):
{"applicable": true 或 false, "element_updates": [{"key": "要素key", "value": "要素摘要", "status": "known" 或 "na"}], "na_keys": ["不涉及的要素key"], "promote_keys": ["升关键的要素key"], "questions": [{"key": "要素key", "question": "一句话反问"}], "done": true 或 false}
```

`prompts.py:85` `MID_CLARIFY_PROMPT` 末尾追加：
```
只输出一个 JSON 对象,字段名必须与下面完全一致(不要输出任何其他文本):
{"question": "一个聚焦追问,律师问诊语气,一句话", "element_key": "追问对应的要素key"}
```

`prompts.py:107` `REPLAN_CHECK_PROMPT` 末尾追加（02 册已验证通过的原文）：
```
只输出一个 JSON 对象,字段名必须与下面完全一致:
{"needs_replan": true 或 false, "reason": "不超过50字的依据", "insufficient_reason": "vague|not_found|error|none 四选一"}
```

- [ ] **Step 2: 四处调用改 json_mode**

`LangGraph_lawApp.py:284`：
```python
            | get_executor_llm().with_structured_output(RiskSchema, method="json_mode")
```
`:358`：`| get_executor_llm().with_structured_output(ElementAssessmentSchema, method="json_mode")`
`:911`：`) | get_executor_llm().with_structured_output(ReplanCheckSchema, method="json_mode")`
`:1001`：`| get_executor_llm().with_structured_output(MidClarifySchema, method="json_mode")`

- [ ] **Step 3: 真实 LLM 直测四条链**

```bash
$PY -c "
import asyncio
from lawApp_LangGraph.LangGraph_lawApp import _schema_models, ingest_node
from lawApp_LangGraph.FastAPI.model import *
from langchain_core.prompts import PromptTemplate
from lawApp_LangGraph.FastAPI.model import *  # noqa
PlanSchema, ReplanCheckSchema, RiskSchema, ElementAssessmentSchema, MidClarifySchema = _schema_models()
from lawApp_LangGraph.prompts import RISK_GATE_PROMPT, ELEMENT_ASSESS_PROMPT, MID_CLARIFY_PROMPT, REPLAN_CHECK_PROMPT
from lawApp_LangGraph.FastAPI.model import *
from lawApp_LangGraph.LangGraph_lawApp import get_executor_llm

async def main():
    v = await (PromptTemplate.from_template(RISK_GATE_PROMPT) | get_executor_llm().with_structured_output(RiskSchema, method='json_mode')).ainvoke({'query': '我打算离婚, 房子婚后买的。'})
    print('Risk:', type(v).__name__, v.high_risk)
    v = await (PromptTemplate.from_template(REPLAN_CHECK_PROMPT) | get_executor_llm().with_structured_output(ReplanCheckSchema, method='json_mode')).ainvoke({'executed': '已检索 3 条案例'})
    print('ReplanCheck:', type(v).__name__, v.needs_replan, v.insufficient_reason)
    v = await (PromptTemplate.from_template(MID_CLARIFY_PROMPT) | get_executor_llm().with_structured_output(MidClarifySchema, method='json_mode')).ainvoke({'missing': '结婚年限, 财产状况'})
    print('MidClarify:', type(v).__name__, v.question[:30])
asyncio.run(main())
"
```
Expected: 三行均打印 Schema 实例名与字段值，无 OutputParserException。（ElementAssessment 链变量较多，由 07 册整链验证，不在此单测。）

- [ ] **Step 4: 回归 02 册**

Run: `$PY tests_ipynb/run_all.py 02 --save`
Expected: json_mode 两条断言 PASS；总结行 `ALL PASSED` 或仅剩环境类 SKIP。

- [ ] **Step 5: commit** — `C: 地基修复 — risk/assess/replan_check/mid_clarify 四节点结构化输出改 json_mode + 提示词字段声明`

---

### Task 2: 批量嵌入 + 案例入库 + PG 密码（P0.4/P0.3）

**Files:**
- Modify: `lawApp_LangGraph/RAG_service/embedder.py`
- Create: `scripts/ingest_cases_pgvector.py`
- Modify(配置): `lawApp_LangGraph/.env`

**Interfaces:**
- Produces: `embedder.embed_documents(texts: list[str]) -> list[list[float]]`（归一化 1024 维，Task 8 之后无依赖但入库脚本依赖）
- Produces: 可执行 `$PY scripts/ingest_cases_pgvector.py`，law_cases 有数据

- [ ] **Step 1: .env 填真实 DB_PASSWORD**（人工/代理改 `lawApp_LangGraph/.env` 的 `DB_PASSWORD=` 行为实际密码；无代码）

- [ ] **Step 2: embedder.py 加批量接口**（在 `embed_query` 之后追加）

```python
def embed_documents_sync(texts: list[str]) -> list[list[float]]:
    """批量嵌入多条文本(归一化),入库脚本用。

    Args:
        texts: 待嵌入文本列表。

    Returns:
        与输入等长的向量列表(每条 1024 维,已归一化)。
    """
    return (
        get_embedder()
        .encode(texts, normalize_embeddings=True, batch_size=32)
        .tolist()
    )


async def embed_documents(texts: list[str]) -> list[list[float]]:
    """embed_documents_sync 的异步包装(线程池执行避免阻塞事件循环)。"""
    import asyncio

    return await asyncio.to_thread(embed_documents_sync, texts)
```

- [ ] **Step 3: 写入库脚本 `scripts/ingest_cases_pgvector.py`**

```python
"""案例语料批量入库 law_cases(pgvector)。

用法: $PY scripts/ingest_cases_pgvector.py [--dry-run]
幂等: 以 id = f"{文件名}::{chunk_index}" 为主键, 重跑覆盖同 id 行(ON CONFLICT DO UPDATE)。
"""
from __future__ import annotations

import asyncio
import re
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHUNK = 500
OVERLAP = 50


def _chunks(text: str) -> list[str]:
    """滑动窗口分块: 500 字/块, 50 字重叠。"""
    step = CHUNK - OVERLAP
    return [text[i : i + CHUNK] for i in range(0, max(len(text) - OVERLAP, 1), step)]


def _parse_meta(path: Path) -> tuple[str, str]:
    """从文件名提取 (year, case_number)。文件名形如 中国法院2014年度案例_婚姻家庭与继承纠纷.md。"""
    m = re.search(r"(19|20)\d{2}", path.stem)
    return (m.group(0) if m else "", path.stem)


async def main(dry: bool = False) -> None:
    """读取 data/Documents/MarkDownFiles 全部案例 md,分块嵌入后写入 law_cases。"""
    from lawApp_LangGraph.RAG_service.embedder import embed_documents_sync
    from lawApp_LangGraph.db import get_pool

    docs_dir = ROOT / "data" / "Documents" / "MarkDownFiles"
    files = sorted(docs_dir.glob("*.md"))
    if not files:
        raise SystemExit(f"未找到案例语料: {docs_dir}")

    prepared: list[tuple] = []
    for f in files:
        year, case_number = _parse_meta(f)
        text = re.sub(r"\s+", " ", f.read_text(encoding="utf-8")).strip()
        for i, chunk in enumerate(_chunks(text)):
            prepared.append((f"{f.stem}::{i}", year, case_number, "", i, chunk))
    if dry:
        print(f"[dry-run] {len(files)} 个文件 → {len(prepared)} 块")
        return

    vectors = embed_documents_sync([p[5] for p in prepared])
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            INSERT INTO law_cases (id, year, case_number, case_cause, chunk_index, chunk_text, embedding)
            VALUES (%s, %s, %s, %s, %s, %s, %s::vector)
            ON CONFLICT (id) DO UPDATE SET chunk_text = EXCLUDED.chunk_text,
                                           embedding = EXCLUDED.embedding
            """,
            [(p[0], p[1], p[2], p[3], p[4], p[5], v) for p, v in zip(prepared, vectors)],
        )
        await conn.commit()
        print(f"入库完成: {cur.rowcount} 行 (来自 {len(files)} 个文件)")


if __name__ == "__main__":
    asyncio.run(main(dry="--dry-run" in sys.argv))
```

注意: `get_pool()` 返回 psycopg_async `AsyncConnectionPool`，`conn.execute` 多行用 `values` 列表参数一次提交；若驱动报参数形式错误，改为逐行 `await cur.execute(sql, row)` 循环（行为等价，保留 conn.commit()）。

- [ ] **Step 4: 执行入库并验证**

```bash
$PY scripts/ingest_cases_pgvector.py --dry-run   # Expected: [dry-run] 11 个文件 → 若干块
$PY scripts/ingest_cases_pgvector.py             # Expected: 入库完成: N 行
$PY -c "
import asyncio
from lawApp_LangGraph.db import get_pool
async def main():
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute('SELECT count(*) FROM law_cases')
        print('law_cases 行数:', (await cur.fetchone())[0])
asyncio.run(main())
"
```
Expected: law_cases 行数 > 100（11 文件 × 多块）。

- [ ] **Step 5: 回归 03/04 册**

Run: `$PY tests_ipynb/run_all.py 03 04 --save`
Expected: 03 册 PG 认证 FAIL 转 PASS；04 册检索三项 SKIP 转 PASS（fetch_laws/get_retriever/RAG 链拿到真实数据）。

- [ ] **Step 6: commit** — `C: 地基修复 — embed_documents 批量嵌入 + 案例语料入库脚本, law_cases 有数据`

---

### Task 3: mode/doc_type 状态 + 文书要素集 + 提示词变体（P1.3 + 双模式地基）

**Files:**
- Modify: `lawApp_LangGraph/state.py`（AgentState 加两字段）
- Modify: `lawApp_LangGraph/prompts.py`（4 个新常量）
- Modify: `lawApp_LangGraph/LangGraph_lawApp.py`（ingest_node 注入 mode；ingest 按模式建要素集）

**Interfaces:**
- Produces: `AgentState.mode: str = "attorney"`、`AgentState.doc_type: str = ""`（Task 5/6 请求体映射到这里）
- Produces: prompts 常量 `PLANNER_ASSISTANT_SUFFIX`、`FINALIZE_COMPLAINT_PROMPT`、`FINALIZE_DEFENSE_PROMPT`、`DISCLAIMER_TEXT`、`DOC_ELEMENT_DEFS`（Task 4/5/6 直接 import）
- 图结构、路由函数、六处 interrupt 零改动

- [ ] **Step 1: state.py AgentState 加字段**（`query: str = ""` 行附近）

```python
    mode: str = "attorney"  # attorney=代理律师咨询 / assistant=律师助理文书起草
    doc_type: str = ""  # assistant 模式: complaint=起诉状 / defense=答辩状
```

- [ ] **Step 2: prompts.py 追加 4 个常量**（文件末尾）

```python
# ============ 双模式(子项目C) ============

# 律师助理模式的规划差异: 只追加在 PLANNER_SYSTEM 之后, 变量名不变
PLANNER_ASSISTANT_SUFFIX = """
本次是【律师助理-文书起草】任务(婚姻家事类): 用户提交了完整案件详情, 目标是起草
{doc_type_label}。规划时优先: ①从案件详情提取文书要素(当事人/诉求/事实/证据)
②检索婚姻家事法条与类案 ③评估材料缺口(缺则反问) ④文书结构化起草。
"""

DISCLAIMER_TEXT = (
    "本系统由 AI 驱动，并非执业律师，输出不构成正式法律意见。"
    "涉及紧急人身安全请立即拨打 110（家暴可拨妇联热线 12338）。"
    "继续使用即表示您已知晓上述限制。"
)

# 文书要素集: assistant 模式下替代婚姻家事要素, 复用 CaseElements 机制
DOC_ELEMENT_DEFS = [
    # (key, label, critical)
    ("parties", "当事人信息(原告/被告姓名与基本情况)", True),
    ("claims", "诉讼请求(离婚/抚养/财产分割等)", True),
    ("facts", "事实与理由(婚姻经过/争议焦点)", True),
    ("evidence", "证据清单", False),
    ("marriage_status", "婚姻现状(登记时间/是否分居)", False),
]

FINALIZE_COMPLAINT_PROMPT = """你是资深婚姻家事律师的助理。根据已收集的要素与检索到的法条/类案,起草一份【民事起诉状】(婚姻家事类)。

严格按以下结构输出(纯文本,不使用 markdown 代码块):
民事起诉状
原告:[姓名/性别/出生年月/民族/住址/联系方式,未知处写"待补充"]
被告:[同上]
诉讼请求:
1. [请求事项,如准予离婚]
2. [如子女抚养/财产分割]
事实与理由:
[婚姻缔结经过/感情变化/分居或家暴等事实,引用检索到的法条条文]
证据清单:
[列证据名称与证明目的]
此致
[人民法院名称]
具状人:[原告姓名] [日期待补充]

末尾必须附加一行: (AI 起草,需执业律师复核后使用)

已知案件要素: {elements_digest}
原始案情: {query}
检索到的法条: {laws_digest}
相关案例要点: {cases_digest}
"""

FINALIZE_DEFENSE_PROMPT = """你是资深婚姻家事律师的助理。根据已收集的要素与检索到的法条/类案,针对原告起诉状起草一份【答辩状】(婚姻家事类)。

严格按以下结构输出(纯文本,不使用 markdown 代码块):
民事答辩状
答辩人:[姓名/基本情况,未知处写"待补充"]
被答辩人(原告):[同上]
答辩意见:
1. [针对原告诉求逐项回应: 事实认定/法律适用]
2. [提出抗辩理由,引用检索到的法条条文]
事实与理由:
[答辩所依据的事实与证据]
证据清单:
[列证据名称与证明目的]
此致
[人民法院名称]
答辩人:[姓名] [日期待补充]

末尾必须附加一行: (AI 起草,需执业律师复核后使用)

已知案件要素: {elements_digest}
原始案情: {query}
检索到的法条: {laws_digest}
相关案例要点: {cases_digest}
"""
```

- [ ] **Step 3: ingest_node 按模式建要素集 + 注入 mode**

`LangGraph_lawApp.py:221` ingest_node：返回 dict 中追加两键，并在函数开头按模式构建 `case_elements`：

```python
def _build_elements(mode: str) -> CaseElements:
    """按模式构建要素集: attorney=婚姻家事要素(现状不动), assistant=文书要素。"""
    from lawApp_LangGraph.prompts import DOC_ELEMENT_DEFS

    if mode == "assistant":
        return CaseElements(
            elements=[
                CaseElement(key=k, label=l, critical=c)
                for k, l, c in DOC_ELEMENT_DEFS
            ]
        )
    return _build_case_elements()  # 既有婚姻家事要素构建(现状函数名以实际为准, 见 ingest 现有实现)
```

若现状是 ingest 内联构建婚姻要素，抽出为 `_build_case_elements()` 再如上分支；ingest_node 返回值追加：

```python
        "mode": state.mode or "attorney",
        "doc_type": state.doc_type or "",
        "case_elements": _build_elements(state.mode or "attorney"),
```

- [ ] **Step 4: 验证 mode 注入**

```bash
$PY -c "
from lawApp_LangGraph.LangGraph_lawApp import ingest_node
from lawApp_LangGraph.state import AgentState
out = ingest_node(AgentState(query='离婚', mode='assistant', doc_type='complaint'))
print(out['mode'], out['doc_type'], [e.key for e in out['case_elements'].elements])
out2 = ingest_node(AgentState(query='离婚'))
print(out2['mode'], [e.key for e in out2['case_elements'].elements][:3])
"
```
Expected: 第一行 `assistant complaint ['parties','claims',...]`；第二行 `attorney` + 既有婚姻要素 key（如 marriage_status 等）。

- [ ] **Step 5: commit** — `C: 双模式地基 — state 增 mode/doc_type, 文书要素集, 起诉状/答辩状/免责提示词, ingest 按模式建要素`

---

### Task 4: planner/replanner 手工流式 + reasoning 总线（P1.2，直接报错）

**Files:**
- Modify: `lawApp_LangGraph/LangGraph_lawApp.py:509-560(planner)`、`:~1060-1105(replanner)`

**Interfaces:**
- Produces: `open_reasoning_channel(thread_id) -> asyncio.Queue`、`close_reasoning_channel(thread_id) -> None`、`_REASONING_BUS`（Task 6 SSE 端点消费）
- Produces: 队列元素格式 `{"source": "planner"|"replanner", "delta": str}`；结束哨兵 `None`
- planner/replanner 解析失败**直接抛错**（不做兜底，用户决策）——planner 现有 try/except 整段删除

- [ ] **Step 1: 模块级 reasoning 总线**（`LangGraph_lawApp.py` 顶部 import 区之后）

```python
# CoT 总线: thread_id → Queue; SSE 端点开道, planner/replanner 推 reasoning 增量
_REASONING_BUS: dict[str, "asyncio.Queue"] = {}


def open_reasoning_channel(thread_id: str) -> "asyncio.Queue":
    """为会话开启 reasoning 通道;SSE 端点在 graph.astream 之前调用。

    Args:
        thread_id: 会话 ID(与 graph_config 的 configurable.thread_id 一致)。

    Returns:
        新建的 Queue; planner/replanner 推 {"source","delta"}, 结束推 None 哨兵。
    """
    import asyncio

    q: asyncio.Queue = asyncio.Queue()
    _REASONING_BUS[thread_id] = q
    return q


def close_reasoning_channel(thread_id: str) -> None:
    """关闭并移除 reasoning 通道(幂等)。"""
    _REASONING_BUS.pop(thread_id, None)
```

- [ ] **Step 2: 流式解析助手**（planner_node 上方）

```python
def _parse_plan_json(raw: str) -> "PlanSchema":
    """解析 reasoner 流式累积的 content 为 PlanSchema;失败直接抛错(不做兜底)。"""
    import json
    import re

    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        raise ValueError(f"Planner 输出中未找到 JSON 对象: {raw[:200]!r}")
    return PlanSchema.model_validate(json.loads(m.group(0)))


async def _stream_plan(prompt_text: str, source: str, config: dict) -> tuple["PlanSchema", list[str]]:
    """手工流式调用 reasoner: reasoning_content 逐字推入 CoT 总线,累积 content 手动解析。

    Args:
        prompt_text: 已 format 好的完整提示词。
        source: 事件来源标记, "planner" 或 "replanner"。
        config: 节点 config, 取 configurable.thread_id 定位总线。

    Returns:
        (解析成功的 PlanSchema, reasoning 增量列表)。

    Raises:
        ValueError: content 无法解析为 JSON 或不符合 PlanSchema 时直接抛出。
    """
    thread_id = (config.get("configurable") or {}).get("thread_id", "")
    q = _REASONING_BUS.get(thread_id)
    reasoning: list[str] = []
    content: list[str] = []
    async for chunk in await get_planner_llm().astream(prompt_text):
        rc = chunk.additional_kwargs.get("reasoning_content", "")
        if rc:
            reasoning.append(rc)
            if q is not None:
                await q.put({"source": source, "delta": rc})
        if chunk.content:
            content.append(chunk.content)
    return _parse_plan_json("".join(content)), reasoning
```

- [ ] **Step 3: planner_node 改签名 + 删兜底**

```python
async def planner_node(state: AgentState, config: dict) -> dict:
    """Pro reasoner 流式: 生成计划 + reasoning_content 推 CoT 总线;解析失败直接报错。"""
    t0 = time.time()
    query = state.query.strip()
    debug.debug("→ 进入 Planner 节点", detail=f"query={query[:80]}")

    if not query:
        debug.info("← Planner 退出", detail="空输入", result="返回默认提示")
        return {
            "plan": [],
            "reasoning": ["无输入"],
            "final_answer": "抱一丝,你能再说一遍吗?",
        }

    template = PLANNER_SYSTEM
    if (state.mode or "attorney") == "assistant":
        from lawApp_LangGraph.prompts import PLANNER_ASSISTANT_SUFFIX

        template = PLANNER_SYSTEM + PLANNER_ASSISTANT_SUFFIX.format(
            doc_type_label="起诉状" if state.doc_type != "defense" else "答辩状"
        )
    prompt = PromptTemplate.from_template(template).format(
        query=query[:3000],
        available_tools=_tools_desc(),
        elements_digest=state.case_elements.digest(),
    )
    result, _cot = await _stream_plan(prompt, "planner", config)
    plan = _normalize_plan(result)
    return {"plan": plan, "reasoning": list(result.reasoning or [])}
```

要点：原 try/except 与硬编码默认计划**整段删除**（用户决策：有错误直接报错）；`PLANNER_SYSTEM` 需在文件尾部追加与 Task 1 相同格式的 JSON 字段声明（`{"reasoning": [...], "plan": [{"step_id","description","tool_name"}]}`），追加文本：

```
只输出一个 JSON 对象(不要输出任何其他文本):
{"reasoning": ["思考过程条目"], "plan": [{"step_id": 1, "description": "步骤描述", "tool_name": "工具名或null"}]}
```

- [ ] **Step 4: replanner_node 同样处理**

签名改 `(state, config)`；`chain = get_planner_llm().with_structured_output(PlanSchema)` 段替换为：

```python
    result, _cot = await _stream_plan(prompt, "replanner", config)
```

`REPLANNER_SYSTEM_PROMPT` 尾部追加与 Step 3 相同的 JSON 字段声明；原 except 分支（默认补充步骤）整段删除。

- [ ] **Step 5: 全链冒烟（真实 LLM，走旧 /ask 端点验证图未断）**

```bash
$PY -c "
import asyncio
from lawApp_LangGraph import runtime

async def main():
    graph = await runtime.setup_runtime()
    state = await graph.ainvoke({'query': '结婚 8 年, 两个孩子, 想离婚, 房子婚后买的怎么分?', 'mode': 'attorney'}, config={'configurable': {'thread_id': 't-plan-test'}})
    print('plan_steps:', [(s.step_id, s.tool_name) for s in (state.get('plan') or [])])
    print('reasoning_head:', (state.get('reasoning') or [''])[0][:60])
    print('answer_len:', len(state.get('final_answer') or ''))
    await runtime.teardown_runtime()

asyncio.run(main())
"
```
Expected: plan_steps 非空且工具名来自真实规划（不再是固定 4 步默认计划的样子）；reasoning 首条不含「默认」「降级」字样；answer_len > 100。若抛 `ValueError: Planner 输出中未找到 JSON 对象` → 检查 Step 3 的字段声明是否追加。

- [ ] **Step 6: 回归 06 册**（旧 /ask 路径 + 两条降级检查）

Run: `$PY tests_ipynb/run_all.py 06 --save`
Expected: `ALL PASSED`，其中「计划环节未走兜底」「答复有检索依据」两条 PASS（Task 2 已入库 + 本任务真规划）。

- [ ] **Step 7: commit** — `C: CoT 流 — planner/replanner 手工流式调 reasoner, reasoning_content 推进程内总线, 解析失败直接报错`

---

### Task 5: 请求模型 + /disclaimer + 双阻塞端点（P1.1）

**Files:**
- Modify: `lawApp_LangGraph/FastAPI/model.py`
- Modify: `lawApp_LangGraph/FastAPI/api.py`

**Interfaces:**
- Produces: `AttorneyAskRequest{query, session_id}`、`AssistantAskRequest{case_details, doc_type, session_id}`（Task 6/8 依赖）
- Produces: `POST /attorney/ask`、`POST /assistant/ask`、`GET /disclaimer`；两 ask 端点每次调 `upsert_session` 记账（Task 7 的 /sessions 数据源）

- [ ] **Step 1: model.py 追加**（顶部已 `from pydantic import BaseModel, Field`，补 `from typing import Literal, Optional`）

```python
class AttorneyAskRequest(BaseModel):
    """代理律师模式咨询请求。"""

    query: str = Field(..., min_length=1, max_length=5000, description="用户法律问题")
    session_id: Optional[str] = Field(default=None, description="续聊会话 ID")


class AssistantAskRequest(BaseModel):
    """律师助理模式文书起草请求(婚姻家事类)。"""

    case_details: str = Field(..., min_length=20, max_length=20000, description="完整案件详情")
    doc_type: Literal["complaint", "defense"] = Field(
        ..., description="complaint=起诉状, defense=答辩状"
    )
    session_id: Optional[str] = Field(default=None, description="续聊会话 ID")
```

- [ ] **Step 2: api.py 加 /disclaimer**

```python
@app.get("/disclaimer")
async def disclaimer():
    """代理律师模式免责声明文本(前端小弹窗内容源, 非阻塞提示)。"""
    from lawApp_LangGraph.prompts import DISCLAIMER_TEXT

    return {"disclaimer": DISCLAIMER_TEXT}
```

- [ ] **Step 3: api.py 加记账 helper + 双端点**（放在 `/ask` 之后；`_safe_audit` 已存在, 仿其形状）

```python
async def _safe_upsert_session(sid: str) -> None:
    """会话登记(sessions 表);失败仅记日志, 不阻断咨询主流程。"""
    try:
        from datetime import datetime

        from lawApp_LangGraph.db import upsert_session

        await upsert_session(sid, user_id=None, meta={}, last_active_at=datetime.now())
    except Exception as e:  # pragma: no cover
        system.warning("sessions 登记失败", detail=str(e)[:100])


@app.post("/attorney/ask", response_model=QueryResponse)
async def attorney_ask(request: AttorneyAskRequest):
    """代理律师模式: 多轮追问案情 → 完整法律咨询答复(阻塞式)。"""
    sid = ensure_session(request.session_id)
    set_session(sid)
    await _safe_upsert_session(sid)
    graph = get_graph()
    try:
        state = await graph.ainvoke(
            {"query": request.query, "mode": "attorney"}, config=graph_config(sid)
        )
    except Exception as e:
        flow.error("流程异常", summary="Graph 执行失败", detail=str(e))
        raise HTTPException(status_code=500, detail=f"Graph 执行失败: {e}")
    return await _finalize_or_interrupt(sid, state)


@app.post("/assistant/ask", response_model=QueryResponse)
async def assistant_ask(request: AssistantAskRequest):
    """律师助理模式: 完整案情 + 文书类型 → 起诉状/答辩状草稿(阻塞式)。"""
    sid = ensure_session(request.session_id)
    set_session(sid)
    await _safe_upsert_session(sid)
    graph = get_graph()
    try:
        state = await graph.ainvoke(
            {
                "query": request.case_details,
                "mode": "assistant",
                "doc_type": request.doc_type,
            },
            config=graph_config(sid),
        )
    except Exception as e:
        flow.error("流程异常", summary="Graph 执行失败", detail=str(e))
        raise HTTPException(status_code=500, detail=f"Graph 执行失败: {e}")
    return await _finalize_or_interrupt(sid, state)
```

- [ ] **Step 4: 旧端点标注 deprecated**——`/ask`、`/ask/stream` 的 docstring 首行加 `(deprecated — 请改用 /attorney/ask|/assistant/ask, 二期移除)`，逻辑不动。

- [ ] **Step 5: finalize 按模式选文书模板**

`LangGraph_lawApp.py:1125` finalize_node 中，assistant 模式且 `state.final_answer` 为空时的直接回答分支前插入模式分流：

```python
    if (state.mode or "attorney") == "assistant" and not state.final_answer:
        from lawApp_LangGraph.prompts import (
            FINALIZE_COMPLAINT_PROMPT,
            FINALIZE_DEFENSE_PROMPT,
        )

        template = (
            FINALIZE_DEFENSE_PROMPT
            if state.doc_type == "defense"
            else FINALIZE_COMPLAINT_PROMPT
        )
        prompt = template.format(
            elements_digest=state.case_elements.digest(),
            query=state.query[:3000],
            laws_digest="\n".join(
                f"{l.law_title} {l.article_number}: {l.content[:80]}"
                for l in (state.law_results or [])[:5]
            )
            or "无",
            cases_digest="\n".join(
                d.chunk_text[:100] for d in (state.rag_documents or [])[:3]
            )
            or "无",
        )
        # 直接复用既有流式生成通道生成文书(flash); 具体调用形态对齐本节点
        # 下方既有的「LLM 直接回答」流式代码, 把 prompt 换成上面的 template 即可
```

（实现时把该节点既有的直接回答 LLM 流式调用复制一份喂 `prompt`，产物写 `final_answer`；不要新造流式机制。）

- [ ] **Step 6: 直测双端点**

```bash
$PY -c "
import httpx, json
# 前置: 另开终端起服务  $PY -m uvicorn lawApp_LangGraph.FastAPI.api:app --port 8000
r = httpx.get('http://127.0.0.1:8000/disclaimer', timeout=10)
print('disclaimer:', r.status_code, len(r.json()['disclaimer']) > 20)
r = httpx.post('http://127.0.0.1:8000/attorney/ask', json={'query': '结婚10年想离婚, 孩子归谁?'}, timeout=600)
d = r.json(); print('attorney:', r.status_code, 'answer_len=', len(d.get('final_answer') or ''), 'interrupt=', bool(d.get('interrupt')))
r = httpx.post('http://127.0.0.1:8000/assistant/ask', json={'case_details': '我与妻子2015年登记结婚, 婚后育有一子。因感情不和分居两年, 现拟起诉离婚, 请求判令婚生子由我抚养, 婚房依法分割。', 'doc_type': 'complaint'}, timeout=600)
d = r.json(); print('assistant:', r.status_code, 'answer_len=', len(d.get('final_answer') or ''))
"
```
Expected: 三个均 200；attorney 有 answer 或 interrupt；assistant 的 answer 含「起诉状」「诉讼请求」结构字样。

- [ ] **Step 7: commit** — `C: 双模式端点 — /attorney/ask 与 /assistant/ask 阻塞式 + /disclaimer + sessions 登记 + finalize 文书模板`

---

### Task 6: SSE 双流端点 + reasoning 事件（P1.2/P1.4）

**Files:**
- Modify: `lawApp_LangGraph/FastAPI/api.py`

**Interfaces:**
- Produces: `GET /attorney/ask/stream`、`GET /assistant/ask/stream`；SSE 事件序列在旧协议上新增 `reasoning`（`{"source","delta"}`）；超长文本（>4000 字）返回 HTTP 413
- Consumes: Task 4 的 `open_reasoning_channel/close_reasoning_channel`

- [ ] **Step 1: 长文本守卫 + 共享流式工厂**（放在 `/ask/stream` 之后）

```python
def _validate_stream_text(text: str, limit: int = 4000) -> None:
    """GET 流式端点的 URL 长度防护(用户决策: 直接报错不截断)。"""
    if len(text) > limit:
        raise HTTPException(
            status_code=413, detail=f"文本过长({len(text)} > {limit} 字), 请分批发送"
        )


async def _mode_stream(
    mode: str, query: str, doc_type: str, session_id: str | None
) -> StreamingResponse:
    """双模式 SSE 流式工厂: 旧 /ask/stream 全事件协议 + reasoning CoT 流。

    Args:
        mode: "attorney" 或 "assistant"。
        query: 提问/案情文本。
        doc_type: assistant 模式的文书类型(attorney 传空)。
        session_id: 续聊会话 ID。

    Returns:
        StreamingResponse(text/event-stream)。
    """
    if not query.strip():
        raise HTTPException(status_code=422, detail="query 不能为空")
    _validate_stream_text(query)
    sid = ensure_session(session_id)
    set_session(sid)
    await _safe_upsert_session(sid)
    config = graph_config(sid)
    graph = get_graph()
    from lawApp_LangGraph.LangGraph_lawApp import (
        close_reasoning_channel,
        open_reasoning_channel,
    )

    inputs = {"query": query, "mode": mode}
    if mode == "assistant":
        inputs["doc_type"] = doc_type

    async def event_stream():
        import asyncio

        out_q: asyncio.Queue = asyncio.Queue()
        reasoning_q = open_reasoning_channel(sid)

        async def pump():
            """把 CoT 总线增量搬进统一输出队列。"""
            while True:
                item = await reasoning_q.get()
                if item is None:
                    break
                await out_q.put(("reasoning", item))

        async def run():
            """消费 graph.astream, 事件形态与既有 /ask/stream 完全一致。"""
            final_state: dict = {}
            seen_steps: set[str] = set()
            try:
                async for stream_mode, chunk in graph.astream(
                    inputs,
                    config=config,
                    stream_mode=["updates", "messages", "values"],
                ):
                    if stream_mode == "messages":
                        msg, meta = chunk
                        content = getattr(msg, "content", "")
                        if (
                            isinstance(content, str)
                            and content.strip()
                            and not getattr(msg, "tool_calls", None)
                        ):
                            await out_q.put(("token", content))
                    elif stream_mode == "updates":
                        for node_name, updates in (chunk or {}).items():
                            if node_name == "executor" and isinstance(updates, dict):
                                for step in updates.get("current_step") and [] or []:
                                    pass  # 占位结束, 下一行起为真实事件, 见下
                            # —— 以下与既有 /ask/stream 的 updates 处理逐行对齐复制 ——
                    elif stream_mode == "values":
                        final_state = chunk or final_state
                # 收尾: interrupt / answer / session_id / done (与 /ask/stream 相同逻辑,
                # 基于 extract_interrupt(final_state 快照) 与 build_response 字段)
                snapshot = await graph.aget_state(config)
                itr = extract_interrupt(snapshot)
                if itr:
                    await out_q.put(("interrupt", itr))
                answer = (final_state.get("final_answer") or "").strip()
                if answer:
                    await out_q.put(("answer", answer))
                await out_q.put(("session_id", sid))
            except Exception as e:
                await out_q.put(("error", str(e)))
            finally:
                await reasoning_q.put(None)
                await out_q.put(None)

        tasks = [asyncio.create_task(run()), asyncio.create_task(pump())]
        try:
            while True:
                item = await out_q.get()
                if item is None:
                    break
                yield sse_event(item[0], item[1])
        finally:
            for t in tasks:
                t.cancel()
            close_reasoning_channel(sid)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

**实现要求**：`run()` 内 updates 分支的事件（tool_call/tool_result/element 面板/prompts_record/final_prompts 等）**逐行对齐既有 `/ask/stream`（api.py:222-312）的同名逻辑复制**，仅把 `yield sse_event(...)` 换成 `await out_q.put((event, data))`；删掉上面代码块中标注「占位结束」的两行示意。

- [ ] **Step 2: 两个薄端点**

```python
@app.get("/attorney/ask/stream")
async def attorney_ask_stream(query: str = "", session_id: str | None = None):
    """代理律师模式 SSE 流式: token/tool/reasoning(CoT)/interrupt/answer/done。"""
    return await _mode_stream("attorney", query, "", session_id)


@app.get("/assistant/ask/stream")
async def assistant_ask_stream(
    case_details: str = "", doc_type: str = "complaint", session_id: str | None = None
):
    """律师助理模式 SSE 流式: CoT + 法条/案例检索过程 + 文书 token 流。"""
    if doc_type not in ("complaint", "defense"):
        raise HTTPException(status_code=422, detail="doc_type 必须为 complaint|defense")
    return await _mode_stream("assistant", case_details, doc_type, session_id)
```

- [ ] **Step 3: 直测 SSE**（服务已起）

```bash
$PY -c "
import httpx, json
url = 'http://127.0.0.1:8000/attorney/ask/stream'
events = {}
with httpx.stream('GET', url, params={'query': '结婚8年想离婚, 两个孩子的抚养权怎么判?'}, timeout=600) as r:
    for line in r.iter_lines():
        if line.startswith('data:'):
            e = json.loads(line[5:]); k = e['event']; events[k] = events.get(k, 0) + 1
            if k == 'reasoning' and events[k] == 1:
                print('reasoning 首帧:', e['data']['source'], e['data']['delta'][:20])
print('事件计数:', events)
"
```
Expected: `事件计数` 含 `reasoning`（≥1，source=planner）、`token`、`done`；无 `error`。

- [ ] **Step 4: 413 守卫**

```bash
$PY -c "
import httpx
r = httpx.get('http://127.0.0.1:8000/attorney/ask/stream', params={'query': '长'*5000}, timeout=10)
print(r.status_code, r.json()['detail'][:20])
"
```
Expected: `413` + 「文本过长」。

- [ ] **Step 5: commit** — `C: 双模式流式 — /attorney|assistant/ask/stream + reasoning CoT 事件 + 413 长文本守卫`

---

### Task 7: /sessions 会话历史端点（P1.1）

**Files:**
- Modify: `lawApp_LangGraph/FastAPI/api.py`

**Interfaces:**
- Produces: `GET /sessions`（最近 50 条: session_id/meta/last_active_at）、`GET /sessions/{sid}`（build_response 全量 + 当前 interrupt）；PG 不可用直接报错（不做降级文案，用户决策）

- [ ] **Step 1: 端点实现**

```python
@app.get("/sessions")
async def list_sessions():
    """会话列表(sessions 表, Task 5 的 _safe_upsert_session 数据源)。"""
    from lawApp_LangGraph.db import get_pool

    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT session_id, meta, last_active_at FROM sessions "
            "ORDER BY last_active_at DESC LIMIT 50"
        )
        rows = await cur.fetchall()
    return [
        {"session_id": r[0], "meta": r[1], "last_active_at": str(r[2])} for r in rows
    ]


@app.get("/sessions/{sid}")
async def get_session(sid: str):
    """单会话详情: 最新快照 + interrupt 状态(等待回复时返回待回答问题)。"""
    graph = get_graph()
    snap = await graph.aget_state(graph_config(sid))
    if not snap or not snap.values:
        raise HTTPException(status_code=404, detail=f"会话 {sid} 不存在")
    response = build_response(snap.values, sid)
    return {**response.model_dump(), "interrupt": extract_interrupt(snap)}
```

- [ ] **Step 2: 直测**

```bash
$PY -c "
import httpx
r = httpx.get('http://127.0.0.1:8000/sessions', timeout=10)
items = r.json(); print('sessions:', r.status_code, len(items))
if items:
    sid = items[0]['session_id']
    r2 = httpx.get(f'http://127.0.0.1:8000/sessions/{sid}', timeout=30)
    d = r2.json(); print('detail:', r2.status_code, 'query=', d.get('query','')[:20], 'interrupt=', bool(d.get('interrupt')))
r3 = httpx.get('http://127.0.0.1:8000/sessions/no-such-id', timeout=10)
print('404:', r3.status_code)
"
```
Expected: sessions 列表 ≥1（Task 5 已登记）；detail 200；404 正常。

- [ ] **Step 3: commit** — `C: 会话历史 — GET /sessions 与 /sessions/{sid}(checkpointer 快照 + sessions 表)`

---

### Task 8: tests_ipynb 07 册（双模式接口实测，不 FakeLLM）

**Files:**
- Create: `scripts/gen_nb07.py`（生成器, 入库）
- Create: `tests_ipynb/07_dual_mode_api.ipynb`（生成产物）

**Interfaces:**
- Produces: 07 册直连真实 uvicorn + 真实 LLM 验证双模式全链（含 Task 5/6/7 全部端点）
- 复用: `tests_ipynb/nbkit.py` 的 Checks/bootstrap/free_port/wait_port

- [ ] **Step 1: 写生成器 `scripts/gen_nb07.py`**

```python
"""生成 tests_ipynb/07_dual_mode_api.ipynb — 双模式接口实测册(真实服务+真实LLM)。

用法: $PY scripts/gen_nb07.py
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NB_DIR = ROOT / "tests_ipynb"

BOOT = '''import asyncio, os, subprocess, sys, time
from pathlib import Path
for cand in (Path.cwd(), *Path.cwd().parents):
    if (cand / "nbkit.py").is_file(): NB_DIR = cand; break
    if (cand / "tests_ipynb" / "nbkit.py").is_file(): NB_DIR = cand / "tests_ipynb"; break
else: raise RuntimeError("未找到 nbkit.py")
sys.path.insert(0, str(NB_DIR))
from nbkit import Checks, bootstrap, free_port, wait_port
ROOT = bootstrap()
checks = Checks("07 双模式接口实测")'''

CELLS = [
    ("md", "## 0. 起真实服务(free_port + uvicorn 子进程)"),
    ("code", BOOT),
    ("code", '''PORT = free_port(9100)
PROC = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "lawApp_LangGraph.FastAPI.api:app",
     "--host", "127.0.0.1", "--port", str(PORT)],
    cwd=str(ROOT),
    env={**os.environ, "PYTHONPATH": str(ROOT), "HF_HOME": r"E:/huggingface_cache", "HF_HUB_OFFLINE": "1"},
)
ok = wait_port("127.0.0.1", PORT, timeout=90)
checks.expect(ok, "uvicorn 起服务", f"port={PORT}" if ok else "90s 未就绪")'''),
    ("md", "## 1. GET /disclaimer"),
    ("code", '''import httpx, json
BASE = f"http://127.0.0.1:{PORT}"
r = httpx.get(f"{BASE}/disclaimer", timeout=10)
d = r.json() if r.status_code == 200 else {}
checks.expect(r.status_code == 200 and len(d.get("disclaimer", "")) > 20,
              "免责声明端点", f"{r.status_code} len={len(d.get('disclaimer',''))}")'''),
    ("md", "## 2. POST /attorney/ask(真实 LLM)"),
    ("code", '''r = httpx.post(f"{BASE}/attorney/ask",
    json={"query": "结婚10年, 两个孩子, 想离婚, 抚养权和房子怎么分?"},
    timeout=600)
d = r.json() if r.status_code == 200 else {}
ATT_SID = d.get("session_id", "")
checks.expect(r.status_code == 200 and (bool(d.get("final_answer")) or bool(d.get("interrupt"))),
              "代理律师模式咨询", f"{r.status_code} answer={len(d.get('final_answer') or '')}字 interrupt={bool(d.get('interrupt'))}")'''),
    ("md", "## 3. POST /assistant/ask(真实 LLM 起诉状)"),
    ("code", '''r = httpx.post(f"{BASE}/assistant/ask",
    json={"case_details": "我与妻子2015年登记结婚, 婚后育有一子。因感情不和分居两年, 现拟起诉离婚, 请求判令婚生子由我抚养, 婚房依法分割。",
          "doc_type": "complaint"},
    timeout=600)
d = r.json() if r.status_code == 200 else {}
ans = d.get("final_answer") or ""
checks.expect(r.status_code == 200 and ("起诉状" in ans and "诉讼请求" in ans),
              "律师助理模式起草起诉状", f"{r.status_code} len={len(ans)} 含结构={('诉讼请求' in ans)}")'''),
    ("md", "## 4. SSE /attorney/ask/stream(含 reasoning CoT)"),
    ("code", '''events, first_reasoning = {}, {}
with httpx.stream("GET", f"{BASE}/attorney/ask/stream",
                  params={"query": "结婚8年想离婚, 孩子抚养权怎么判?", "session_id": ""},
                  timeout=600) as r:
    for line in r.iter_lines():
        if line.startswith("data:"):
            e = json.loads(line[5:]); events[e["event"]] = events.get(e["event"], 0) + 1
            if e["event"] == "reasoning" and "source" not in first_reasoning:
                first_reasoning = e["data"]
checks.expect("reasoning" in events and "done" in events and "error" not in events,
              "SSE 含 CoT 流", f"reasoning={events.get('reasoning',0)}帧 done={'done' in events} err={'error' in events}")
checks.expect("token" in events, "SSE token 流", f"token={events.get('token',0)}帧")'''),
    ("md", "## 5. 413 长文本守卫 / 422 doc_type / 404 会话"),
    ("code", '''r = httpx.get(f"{BASE}/attorney/ask/stream", params={"query": "长" * 5000}, timeout=10)
checks.expect(r.status_code == 413, "413 长文本守卫", str(r.status_code))
r = httpx.get(f"{BASE}/assistant/ask/stream", params={"case_details": "x" * 30, "doc_type": "bad"}, timeout=10)
checks.expect(r.status_code == 422, "422 doc_type 校验", str(r.status_code))
r = httpx.get(f"{BASE}/sessions/no-such-id", timeout=10)
checks.expect(r.status_code == 404, "404 会话不存在", str(r.status_code))'''),
    ("md", "## 6. /sessions 列表含新会话"),
    ("code", '''r = httpx.get(f"{BASE}/sessions", timeout=10)
items = r.json() if r.status_code == 200 else []
checks.expect(r.status_code == 200 and any(i["session_id"] == ATT_SID for i in items),
              "会话登记与列表", f"{r.status_code} 共{len(items)}条 含本次={any(i['session_id']==ATT_SID for i in items)}")'''),
    ("md", "## 7. 清理"),
    ("code", '''PROC.terminate()
try:
    PROC.wait(timeout=10)
except subprocess.TimeoutExpired:
    PROC.kill()
print(checks.report())'''),
]


def cell(kind: str, src: str, idx: int) -> dict:
    if kind == "md":
        return {"cell_type": "markdown", "metadata": {}, "source": src}
    return {
        "cell_type": "code", "execution_count": None, "metadata": {},
        "outputs": [], "source": src,
    }


nb = {
    "nbformat": 4, "nbformat_minor": 5,
    "metadata": {
        "kernelspec": {"display_name": "lawapp", "language": "python", "name": "lawapp"},
    },
    "cells": [cell(k, s, i) for i, (k, s) in enumerate(CELLS)],
}
out = NB_DIR / "07_dual_mode_api.ipynb"
out.write_text(json.dumps(nb, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"OK: {out}")
```

注意: `nbkit.free_port/wait_port` 签名以 `tests_ipynb/nbkit.py` 实际为准（05 册已用过，直接照抄其调用形态）；`wait_port` 返回值语义（bool/None）同样对齐 05 册用法。

- [ ] **Step 2: 生成 + 执行**

```bash
$PY scripts/gen_nb07.py
$PY tests_ipynb/run_all.py 07 --save
```
Expected: 07 册 `ALL PASSED`（8+ 条 PASS：服务/disclaimer/双模式/SSE CoT/守卫/sessions）。

- [ ] **Step 3: commit** — `C: 测试 — 07 册双模式接口实测(真实服务+真实LLM, 不用替身)`

---

### Task 9: 前端迁移 + Tailwind v4 + axios + Inspira 基建（P2 开局）

**Files:**
- Move: `lawApp_LangGraph/law_agent_Vue/` → `frontend/`（仓库根；删除其中 `dist/`，node_modules 保留）
- Modify: `frontend/package.json`、`frontend/vite.config.js`、`frontend/src/main.js`
- Create: `frontend/src/style.css`

**Interfaces:**
- Produces: `frontend/` 可 `npm run dev`（proxy 不变）与 `npm run build`；`npm i` 装齐 tailwindcss@4 / @tailwindcss/vite / axios / motion-v / lucide-vue-next / reka-ui / clsx / tailwind-merge

- [ ] **Step 1: 迁移**

```bash
mv lawApp_LangGraph/law_agent_Vue frontend
rm -rf frontend/dist
```
（`law_agent_Vue` 未跟踪，直接 mv；Python 包内不再残留 52MB 前端产物。）

- [ ] **Step 2: 装依赖**

```bash
cd frontend
npm install axios motion-v lucide-vue-next reka-ui clsx tailwind-merge
npm install -D tailwindcss @tailwindcss/vite
```

- [ ] **Step 3: vite.config.js 加 Tailwind 插件**（proxy 块不动）

```js
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [vue(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
```

- [ ] **Step 4: style.css + main.js**

`frontend/src/style.css`：
```css
@import "tailwindcss";

/* 引用来源专用衬线体(用户决策: 字体区别于正文) */
.font-citation {
  font-family: "Noto Serif SC", "Songti SC", SimSun, serif;
}
```
`frontend/src/main.js`：
```js
import { createApp } from 'vue'
import App from './App.vue'
import './style.css'

createApp(App).mount('#app')
```

- [ ] **Step 5: 构建验证**

```bash
cd frontend && npm run build
```
Expected: `vite build` 零错误退出。

- [ ] **Step 6: commit** — `C: 前端基建 — 迁出 Python 包至 frontend/, Tailwind v4 + axios + Inspira 依赖族, 构建零错`

---

### Task 10: 前端数据层（api.js / sse.js / store.js）

**Files:**
- Create: `frontend/src/api.js`、`frontend/src/sse.js`、`frontend/src/store.js`

**Interfaces:**
- Produces: `askAttorney(query, sessionId)` / `askAssistant(caseDetails, docType, sessionId)` / `resumeHITL(sessionId, answer)` / `listSessions()` / `getSessionDetail(sid)` / `fetchDisclaimer()`（全部 Promise）
- Produces: `streamConsult(url, onEvent, signal)`（SSE 帧解析器，Task 11 消费）
- Produces: store 的 `state.mode/state.sessionId/state.messages/state.reasoning/state.interrupt/state.elements` + `useTypewriter()`

- [ ] **Step 1: api.js（axios，走 vite proxy 的 /api 前缀）**

```js
import axios from 'axios'

const http = axios.create({ baseURL: '/api', timeout: 600000 })

export const askAttorney = (query, sessionId = '') =>
  http.post('/attorney/ask', { query, session_id: sessionId }).then((r) => r.data)

export const askAssistant = (caseDetails, docType, sessionId = '') =>
  http
    .post('/assistant/ask', {
      case_details: caseDetails,
      doc_type: docType,
      session_id: sessionId,
    })
    .then((r) => r.data)

export const resumeHITL = (sessionId, answer) =>
  http.post('/ask/resume', { session_id: sessionId, answer }).then((r) => r.data)

export const listSessions = () => http.get('/sessions').then((r) => r.data)

export const getSessionDetail = (sid) =>
  http.get(`/sessions/${sid}`).then((r) => r.data)

export const fetchDisclaimer = () =>
  http.get('/disclaimer').then((r) => r.data.disclaimer)
```

- [ ] **Step 2: sse.js（fetch 流读；帧格式 `data: {"event":E,"data":D}\n\n`，见 utils.sse_event）**

```js
// SSE 帧解析: 后端每帧是单行 "data: {json}\n\n" (utils.sse_event)
export async function streamConsult(url, onEvent, signal) {
  const res = await fetch(url, { signal })
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      detail = (await res.json()).detail || detail
    } catch { /* 非 JSON 错误体, 保留状态码 */ }
    throw new Error(detail)
  }
  const reader = res.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buf = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let idx
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const frame = buf.slice(0, idx)
      buf = buf.slice(idx + 2)
      const dataLine = frame.split('\n').find((l) => l.startsWith('data:'))
      if (dataLine) onEvent(JSON.parse(dataLine.slice(5).trim()))
    }
  }
}
```

- [ ] **Step 3: store.js（reactive + localStorage 会话 + 打字机 composable）**

```js
import { ref } from 'vue'

export const state = ref({
  mode: 'attorney',          // attorney | assistant
  sessionId: localStorage.getItem('lawapp_sid') || '',
  disclaimerShown: localStorage.getItem('lawapp_disclaimer') === '1',
  messages: [],              // {role:'user'|'assistant', text, collapsed, done}
  reasoning: '',             // CoT 累积文本
  reasoningActive: false,
  reasoningError: false,
  tools: [],                 // 工具时间线 {name, result}
  elements: [],              // 要素面板
  interrupt: null,           // 当前 HITL 载荷
  busy: false,
  error: '',
})

export function setSession(sid) {
  state.value.sessionId = sid
  if (sid) localStorage.setItem('lawapp_sid', sid)
}

export function markDisclaimerShown() {
  state.value.disclaimerShown = true
  localStorage.setItem('lawapp_disclaimer', '1')
}

export function resetTurn() {
  Object.assign(state.value, {
    reasoning: '', reasoningActive: false, reasoningError: false,
    tools: [], elements: [], interrupt: null, error: '',
  })
}

// 打字机效果(用户决策): token 流入 full, 显示层逐字追平
export function useTypewriter(full, shown, cps = 60) {
  let timer = null
  function start() {
    if (timer) return
    timer = setInterval(() => {
      if (shown.value.length >= full.value.length) {
        clearInterval(timer); timer = null; return
      }
      shown.value = full.value.slice(0, shown.value.length + 1)
    }, 1000 / cps)
  }
  return { start }
}
```

- [ ] **Step 4: 构建验证**

```bash
cd frontend && npm run build
```
Expected: 零错误（App.vue 此时尚未引用新模块，纯语法检查）。

- [ ] **Step 5: commit** — `C: 前端数据层 — axios api.js + fetch SSE 解析器 + reactive store(会话续聊/打字机)`

---

### Task 11: 前端组件 + 单界面组装（P2 主体）

**Files:**
- Create: `frontend/src/components/` 下 10 个组件（见下）
- Modify: `frontend/src/App.vue`（整体重写为单界面）

**Interfaces:**
- Consumes: Task 10 全部导出
- Produces: 可运行的两模式聊天应用；Inspira 组件按官方文档复制安装到 `frontend/src/components/inspira/`（npm 无包，复制即用）

- [ ] **Step 1: 安装 Inspira 基础组件**

从 https://inspira-ui.com 各组件页复制源码到 `frontend/src/components/inspira/`（shadcn-vue 式），本期至少取：`DotPattern`（背景）、`ShimmerButton`（发送）、`TypingAnimation`（打字机光标）、`Timeline`（工具时间线）、`FadeIn`（消息入场）。每个组件文件头部保留 Inspira 的版权注释；页面缺失的组件（如 ChatGroup/BorderBeam）用下方手写实现替代，不强求凑齐。

- [ ] **Step 2: 核心交互三件（给出完整最小实现，样式走 Tailwind class）**

`InterruptPanel.vue`（六类分型 + 自助输入，用户决策）：

```vue
<script setup>
import { ref } from 'vue'
import { resumeHITL, setSession } from '../store'
import { resumeHITL as callResume } from '../api'
const props = defineProps({ interrupt: { type: Object, required: true } })
const emit = defineEmits(['resumed'])
const custom = ref('')
const busy = ref(false)

const TYPE_UI = {
  risk_confirm: { kind: 'confirm', label: '高风险确认' },
  clarify: { kind: 'text', label: '案情澄清' },
  mid_clarify: { kind: 'text', label: '补充追问' },
  pdf_confirm: { kind: 'confirm', label: 'PDF 导出' },
  degrade_confirm: { kind: 'choice', label: '工具降级', options: ['重试', '跳过', '终止'] },
  budget_confirm: { kind: 'choice', label: '预算确认', options: ['收尾'] },
}

async function send(answer) {
  if (!answer || busy.value) return
  busy.value = true
  const r = await callResume(props.interrupt.session_id || state.sessionId, answer)
  busy.value = false
  setSession(r.session_id)
  emit('resumed', r)
}
</script>

<template>
  <div class="border border-amber-300 rounded-xl p-4 my-2 bg-amber-50">
    <div class="text-sm font-semibold text-amber-800">
      {{ TYPE_UI[interrupt.type]?.label || interrupt.type }}
    </div>
    <p class="text-sm my-2">
      {{ interrupt.message || interrupt.question || '请补充信息' }}
    </p>
    <div class="flex gap-2 flex-wrap my-2">
      <button v-if="TYPE_UI[interrupt.type]?.kind === 'confirm'" class="px-3 py-1 rounded bg-amber-600 text-white" @click="send('是')">继续</button>
      <button v-if="TYPE_UI[interrupt.type]?.kind === 'confirm'" class="px-3 py-1 rounded border" @click="send('否')">中止</button>
      <button v-for="o in TYPE_UI[interrupt.type]?.options || []" :key="o"
              class="px-3 py-1 rounded border" @click="send(o)">{{ o }}</button>
    </div>
    <div class="flex gap-2">
      <input v-model="custom" class="flex-1 border rounded px-2 py-1 text-sm"
             placeholder="自助输入: 直接输入你的回答/补充内容" @keyup.enter="send(custom)" />
      <button class="px-3 py-1 rounded bg-amber-600 text-white text-sm" :disabled="busy" @click="send(custom)">发送</button>
    </div>
  </div>
</template>
```

（`script` 里 `state` 未 import 属笔误警示——实现时首行补 `import { state } from '../store'`，并删掉 `resumeHITL` 从 store 的错误导入。）

`ThinkingPanel.vue`（CoT 折叠 + 逐字流）：

```vue
<script setup>
import { computed, ref } from 'vue'
import { state } from '../store'
const open = ref(true)
const head = computed(() => state.value.reasoning.slice(-60))
</script>

<template>
  <details :open="open" class="border rounded-lg p-2 my-1 text-xs bg-slate-50"
           :class="state.reasoningError ? 'border-red-400' : 'border-slate-200'">
    <summary class="cursor-pointer select-none">
      {{ state.reasoningError ? '思考过程(中断, 已保留片段)' : '思考过程' }}
      <span v-if="state.reasoningActive" class="animate-pulse">▍</span>
    </summary>
    <pre class="whitespace-pre-wrap mt-1 text-slate-600">{{ state.reasoning || head }}</pre>
  </details>
</template>
```

`CitationList.vue`（引用衬线体，用户决策）：

```vue
<script setup>
defineProps({ sources: { type: Array, default: () => [] } })
</script>

<template>
  <div v-if="sources.length" class="mt-2">
    <div class="text-xs text-slate-500 mb-1">引用来源</div>
    <ul>
      <li v-for="(s, i) in sources" :key="i" class="font-citation text-xs text-slate-700 leading-5">
        [{{ i + 1 }}] {{ s.title || s.law_title || s.case_number }} {{ s.content || s.snippet || '' }}
      </li>
    </ul>
  </div>
</template>
```

- [ ] **Step 3: 其余组件**（按相同粒度实现，Tailwind class 即可，不引 UI 框架）

- `ModeSwitch.vue`：两枚按钮 `代理律师`/`律师助理`，点击只改 `state.mode` 并 `resetTurn()`（单界面，用户决策）
- `DisclaimerToast.vue`：`onMounted` 调 `fetchDisclaimer()`，`state.disclaimerShown` 为 false 时展示右上角小弹窗（非阻塞，`markDisclaimerShown()` 关闭即不再弹）
- `ChatView.vue`：渲染 `state.messages`；用户消息超 120 字默认 `collapsed`，点击展开（用户决策「提问可折叠」）；assistant 消息用 `useTypewriter` + `▍` 光标
- `ToolTimeline.vue`：`state.tools` 列表，行式时间线
- `ElementPanel.vue`：`state.elements` 渲染 key/label/status/value
- `DocComposer.vue`：assistant 模式输入区——`<textarea>` 案情粘贴 + `起诉状/答辩状` 单选 + 提交按钮（ShimmerButton）
- `HistorySidebar.vue`：`onMounted` 调 `listSessions()`，点击某条 `getSessionDetail(sid)` 展开回看

- [ ] **Step 4: App.vue 单界面组装**（重写 59 行骨架）

```vue
<script setup>
import { onMounted } from 'vue'
import { state, resetTurn, setSession } from './store'
import { askAttorney, askAssistant, streamConsult } from './api'
import ModeSwitch from './components/ModeSwitch.vue'
import DisclaimerToast from './components/DisclaimerToast.vue'
import HistorySidebar from './components/HistorySidebar.vue'
import ChatView from './components/ChatView.vue'
import ThinkingPanel from './components/ThinkingPanel.vue'
import ToolTimeline from './components/ToolTimeline.vue'
import ElementPanel from './components/ElementPanel.vue'
import CitationList from './components/CitationList.vue'
import InterruptPanel from './components/InterruptPanel.vue'
import DocComposer from './components/DocComposer.vue'

onMounted(() => { /* HistorySidebar 数据拉取由其自身 onMounted 负责 */ })

async function submit({ text, docType }) {
  resetTurn(); state.busy = true; state.error = ''
  state.messages.push({ role: 'user', text, collapsed: text.length > 120, done: true })
  state.messages.push({ role: 'assistant', text: '', done: false })
  const isAttorney = state.mode === 'attorney'
  const url = isAttorney
    ? `/api/attorney/ask/stream?query=${encodeURIComponent(text)}&session_id=${encodeURIComponent(state.sessionId || '')}`
    : `/api/assistant/ask/stream?case_details=${encodeURIComponent(text)}&doc_type=${docType || 'complaint'}&session_id=${encodeURIComponent(state.sessionId || '')}`
  const assistant = state.messages[state.messages.length - 1]
  try {
    await streamConsult(url, (e) => {
      if (e.event === 'reasoning') { state.reasoningActive = true; state.reasoning += e.data.delta }
      else if (e.event === 'token') assistant.text += e.data
      else if (e.event === 'tool_call') state.tools.push({ name: e.data })
      else if (e.event === 'tool_result') state.tools[state.tools.length - 1] && (state.tools[state.tools.length - 1].result = e.data)
      else if (e.event === 'interrupt') { state.interrupt = e.data; assistant.done = true }
      else if (e.event === 'answer') assistant.text = e.data
      else if (e.event === 'session_id') setSession(e.data)
      else if (e.event === 'error') { state.error = String(e.data); state.reasoningError = true }
      else if (e.event === 'done') { assistant.done = true; state.reasoningActive = false }
    })
  } catch (err) {
    state.error = String(err)
  } finally {
    state.busy = false; state.reasoningActive = false; assistant.done = true
  }
}
</script>

<template>
  <div class="flex h-screen">
    <HistorySidebar class="w-64 shrink-0 border-r" />
    <div class="flex-1 flex flex-col">
      <header class="flex items-center gap-3 p-3 border-b">
        <ModeSwitch />
        <span class="text-sm text-slate-400">会话: {{ state.sessionId || '(新建)' }}</span>
      </header>
      <DisclaimerToast />
      <main class="flex-1 overflow-y-auto p-4">
        <p v-if="state.error" class="text-red-600 text-sm border border-red-300 rounded p-2">
          出错: {{ state.error }} (不做兜底, 请修正后重试)
        </p>
        <ThinkingPanel />
        <ToolTimeline />
        <ChatView />
        <ElementPanel />
        <InterruptPanel v-if="state.interrupt" :interrupt="state.interrupt" @resumed="onResumed" />
      </main>
      <footer class="border-t p-3">
        <DocComposer v-if="state.mode === 'assistant'" @submit="submit" />
        <!-- attorney: 复用 DocComposer 的同款提交区, 由 ChatView 内的输入框触发 submit; 实现时二选一统一 -->
      </footer>
    </div>
  </div>
</template>
```

（`onResumed` 处理函数: resume 返回后若带新 interrupt 继续挂面板，否则把 `final_answer` 追加为 assistant 消息——在实现时补全该函数，上面 script 块加：

```js
function onResumed(r) {
  state.interrupt = r.interrupt || null
  if (r.final_answer) state.messages.push({ role: 'assistant', text: r.final_answer, collapsed: false, done: true })
  if (r.session_id) setSession(r.session_id)
}
```

- [ ] **Step 5: build + 手工联调**

```bash
cd frontend && npm run build   # Expected: 零错误
npm run dev                    # 起前端 5173; 后端另起 uvicorn 8000
```
手工走查清单（浏览器 http://localhost:5173）：①切到代理律师模式，首见免责小弹窗且可关 ②提问一条婚姻问题，看到 CoT 逐字 + token 打字机 + 工具时间线 ③（若出 interrupt）面板选预设或自助输入，续答 ④切律师助理模式，粘贴案情起草诉状，引用区为衬线字体 ⑤长提问默认折叠可展开 ⑥左侧历史可见本次会话。

- [ ] **Step 6: commit** — `C: 前端主体 — 单界面双模式(按钮切换) + CoT/打字机/折叠/衬线引用/HITL 自助输入 + Inspira 组件`

---

### Task 12: 全量回归 + 文档同步

**Files:**
- Modify: `tests_ipynb/README.md`、`README.md`、`docs/PROJECT_OVERVIEW.md`（目录树/接口表/运行命令）

- [ ] **Step 1: 后端回归** — `$PY tests_ipynb/run_all.py --save` 全 12 册；存量 pytest 不动不跑新增（约束）：`$PY -m pytest tests/ -q` 现有 15 用例不回归。
Expected: 01-07 册无新增 FAIL；06 册两条降级检查 PASS。

- [ ] **Step 2: 前端** — `cd frontend && npm run build` 零错。

- [ ] **Step 3: 文档三处**：①README 增 `frontend/` 启动方式（npm i/dev/build + vite proxy 说明）②PROJECT_OVERVIEW 接口表增 `/attorney/*` `/assistant/*` `/sessions*` `/disclaimer`（旧的标 deprecated）③tests_ipynb/README 增 07 册行。

- [ ] **Step 4: commit** — `C: 收尾 — 全量回归 + README/PROJECT_OVERVIEW/tests_ipynb 文档同步`

---

## Self-Review 记录

- **Spec 覆盖**: P0.1→T1, P0.2→T4, P0.3→T2, P0.4→T2, P1.1→T5/6/7, P1.2→T4/6, P1.3→T3/5, P1.4→T6, Phase 2→T9/10/11, 验证→T8/12, 8 条用户决策全部有落点（axios→T10, 自助输入→T11, 单界面→T11, 非阻塞免责→T5/T11, 折叠→T11, 衬线→T9/T11, 打字机→T10/T11, ipynb不FakeLLM→T8, 直接报错→T4/T6）。
- **占位符**: T6 Step 1 的 updates 事件块与 T11 Step 4 尾部标注了「对齐既有实现补全」的两处，均为「复制现有代码到指定位置」的明确指令而非 TBD；其余步骤全部含完整代码。
- **类型一致**: `AttorneyAskRequest/AssistantAskRequest`(T5) ↔ api.js(T10) 字段名一致；`reasoning` 事件 `{"source","delta"}`(T4) ↔ sse.js/store.js(T10/T11) 一致；`upsert_session`(T5) ↔ `/sessions`(T7) 表列一致；`graph_config(sid).thread_id == sid`(已核实) ↔ `_REASONING_BUS` 键控(T4/T6) 一致。
