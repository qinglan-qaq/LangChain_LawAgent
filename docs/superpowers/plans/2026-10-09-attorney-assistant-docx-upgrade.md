# 律师助理模式升级(docx 双文书/PDF/field_clarify/弹窗/折叠/监控)实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 律师助理模式上线答辩状 Word 模板、生成前关键字段循环补全(field_clarify)、docx/pdf 双格式下载模态弹窗、案情长文渐变折叠、监控页触顶列改运行时长列。

**Architecture:** 增量扩展,不动图拓扑。executor HITL-4 块内 while 循环 + `interrupt()` 多次调用重放实现字段补全;PDF 走下载时按需转换(docx2pdf/Word COM)+ mtime 缓存;前端模态替换既有 8s toast;折叠升级为限高+mask 渐变。

**Tech Stack:** LangGraph(interrupt 重放)/ docxtpl / docx2pdf(Word COM)/ FastAPI / Vue3 + Vite + Tailwind / pytest。

**Spec:** `docs/superpowers/specs/2026-10-09-attorney-assistant-docx-upgrade-design.md`

## Global Constraints

- Python 一律用 `F:/Anaconda_env/lawApp_LangGraph/python.exe`(下称 `$PY`);pytest 全量 `$PY -m pytest tests/ -q`(当前基线 197 PASS 0 FAIL)。
- 后端启动端口 8200(8000 在 Windows 端口保留段 7212-8195 内,bind errno 13):`$PY -m uvicorn lawApp_LangGraph.FastAPI.api:app --host 127.0.0.1 --port 8200 --loop lawApp_LangGraph.FastAPI.loop:selector_loop_factory`。
- 前端启动:`cd frontend && BACKEND_PORT=8200 npm run dev`;构建校验 `npm run build` 零错误。
- 代码注释风格:中文、说明约束而非流水账,与周边一致;提交信息格式沿用仓库惯例(中文描述)。
- 测试涉及 PG/docx 落库的用例沿用 `tests/test_docx_generation.py` 的 `_skip_if_no_pg()` / `_docx_cleanup()` 模式。
- 前端 DOM 唯一 id + 语义 class 惯例必须遵守(全前端框已上过 id)。

---

### Task 1: 状态与配置基础(doc_field_rounds / max_doc_field_rounds / doc_label)

**Files:**
- Modify: `lawApp_LangGraph/state.py:337-345`(docx 相关字段旁)
- Modify: `lawApp_LangGraph/config.py:35`(max_clarify_rounds 旁)
- Modify: `lawApp_LangGraph/doc_templates.py`(新增 `doc_label`)
- Modify: `lawApp_LangGraph/LangGraph_lawApp.py:403-450`(ingest_node)
- Test: `tests/test_field_clarify.py`(新建)

**Interfaces:**
- Produces: `settings.max_doc_field_rounds: int = 3`(env 同名覆盖);`AgentState.doc_field_rounds: int = 0`;`doc_templates.doc_label(doc_type: str) -> str`(complaint→"民事起诉状", defense→"民事答辩状", 未知→"Word 文书")。Task 2/3/5 依赖这三个名字。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_field_clarify.py
"""field_clarify 字段补全循环 — 状态/配置/循环/落库 全链测试。"""
import types


def test_doc_field_rounds_defaults_and_reset():
    """state 新字段默认 0; ingest 重置后仍为 0(显式写入便于审计)。"""
    from lawApp_LangGraph.LangGraph_lawApp import ingest_node
    from lawApp_LangGraph.state import AgentState

    st = AgentState(query="q", mode="assistant", doc_type="complaint",
                    doc_field_rounds=2)
    upd = ingest_node(st)
    assert upd["doc_field_rounds"] == 0


def test_max_doc_field_rounds_config():
    from lawApp_LangGraph.config import settings
    assert settings.max_doc_field_rounds == 3


def test_doc_label():
    from lawApp_LangGraph.doc_templates import doc_label
    assert doc_label("complaint") == "民事起诉状"
    assert doc_label("defense") == "民事答辩状"
    assert doc_label("unknown") == "Word 文书"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `$PY -m pytest tests/test_field_clarify.py -v`
Expected: FAIL — `AttributeError: ... 'AgentState' object has no attribute 'doc_field_rounds'` / `ImportError: cannot import name 'doc_label'`

- [ ] **Step 3: 实现**

`state.py`(docx 三件套所在行,clarify_rounds 附近加):

```python
    # field_clarify 关键字段补问轮数(ingest 归零; executor 写入)
    doc_field_rounds: int = 0
```

`config.py`(max_clarify_rounds 下一行):

```python
    # docx 生成前关键字段补问轮数上限(field_clarify)
    max_doc_field_rounds: int = 3
```

`doc_templates.py`(load_fields 之后新增;fields.yaml 顶层有 `label:` 键):

```python
def doc_label(doc_type: str) -> str:
    """文书显示名(fields.yaml 顶层 label; 缺模板/缺键给通用兜底)。"""
    p = _TEMPLATE_ROOT / doc_type / "fields.yaml"
    if not p.exists():
        return "Word 文书"
    try:
        spec = yaml.safe_load(p.read_text(encoding="utf-8"))
        return str(spec.get("label") or "Word 文书")
    except Exception:
        return "Word 文书"
```

`LangGraph_lawApp.py` ingest_node 返回 dict 的 docx 三件套行后加:

```python
        "doc_field_rounds": 0,
```

- [ ] **Step 4: 跑测试确认通过**

Run: `$PY -m pytest tests/test_field_clarify.py -v`
Expected: 3 PASS

- [ ] **Step 5: 提交**

```bash
git add lawApp_LangGraph/state.py lawApp_LangGraph/config.py lawApp_LangGraph/doc_templates.py lawApp_LangGraph/LangGraph_lawApp.py tests/test_field_clarify.py
git commit -m "feat: field_clarify 状态/配置基础 — doc_field_rounds 字段+ingest 重置, max_doc_field_rounds=3, doc_templates.doc_label"
```

---

### Task 2: field_clarify 补全循环(executor while + 落库 + 前端面板接线)

**Files:**
- Modify: `lawApp_LangGraph/LangGraph_lawApp.py:1166-1209`(`_extract_doc_fields` 加 extra_qa 参数)
- Modify: `lawApp_LangGraph/LangGraph_lawApp.py:1292-1340`(HITL-4 块,插入 while 循环;docx_msg 通用化)
- Modify: `lawApp_LangGraph/LangGraph_lawApp.py:1415-1416`(确认返回 updates 加 doc_field_rounds)
- Modify: `lawApp_LangGraph/FastAPI/utils.py:196-216`(normalize_resume docstring 增列 field_clarify,行为不变)
- Modify: `frontend/src/components/InterruptPanel.vue:9-27`(TYPE_LABEL/TEXT_KINDS/PASS_VALUE)
- Test: `tests/test_field_clarify.py`(追加)

**Interfaces:**
- Consumes: Task 1 的 `settings.max_doc_field_rounds` / `doc_label` / `AgentState.doc_field_rounds`。
- Produces: interrupt 载荷 `{"type": "field_clarify", "round": "n/3", "message": str, "field_preview": [{key,label,value,critical,status}]}`;dialogue 事件 `field_question`(payload: round/question/fields, dedupe_on=("round","question"))与 `field_answer`(payload: round/question/answer, dedupe_on=("round","question"));`_extract_doc_fields(state, doc_type, extra_qa="")` 签名。Task 6 的端到端依赖此载荷形状。

- [ ] **Step 1: 写失败测试**(追加到 `tests/test_field_clarify.py`;测试骨架复制 `tests/test_docx_generation.py` 的 `_build_docx_graph` 模式 — 见该文件 330-466 行,LLM 全替身 + `_stream_plan` 单步 generate_docx + `_extract_doc_fields` AsyncMock)

```python
import asyncio
from unittest.mock import AsyncMock

_FIELD_STUB = {  # 关键字段 plaintiff_name 有值, plaintiff_gender 缺(critical)
    "plaintiff_name": "张三", "plaintiff_gender": "", "defendant_name": "李四",
    "defendant_gender": "女", "fact_divorce_reason": "感情不和",
    "fact_basis": "民法典第1079条", "signer": "", "service_addr": "xx路1号",
}


def _build_graph(monkeypatch, fields_seq):
    """fields_seq: 每次抽取调用返回的字段 dict 列表(逐轮模拟补全)。"""
    import lawApp_LangGraph.LangGraph_lawApp as app
    from langchain_core.runnables import Runnable
    from langgraph.checkpoint.memory import MemorySaver

    verdict = types.SimpleNamespace(
        high_risk=False, question_category="marriage_legal", applicable=True,
        element_updates=[], na_keys=[], promote_keys=[], questions=[], done=True,
        needs_replan=False, reason="", insufficient_reason="none",
    )

    class _Chain(Runnable):
        def invoke(self, _i, config=None, **_k): return verdict
        async def ainvoke(self, _i, config=None, **_k): return verdict

    class _StubLLM(Runnable):
        def with_structured_output(self, _s, **_k): return _Chain()
        def bind_tools(self, _t): return self
        def invoke(self, _i, config=None, **_k): return verdict
        async def ainvoke(self, _i, config=None, **_k): return verdict
        async def astream(self, _p, config=None, **_k):
            yield types.SimpleNamespace(content="文书终答测试")

    async def _fake_stream_plan(_p, _s, _c):
        plan = app.PlanSchema.model_validate({
            "reasoning": ["起草"], "plan": [{
                "step_id": 1, "description": "生成文书",
                "tool_name": "generate_docx"}]})
        return plan, []

    calls = {"n": 0}

    async def _fake_extract(state, doc_type, extra_qa=""):
        f = fields_seq[min(calls["n"], len(fields_seq) - 1)]
        calls["n"] += 1
        return dict(f)

    monkeypatch.setattr(app, "get_executor_llm", lambda: _StubLLM())
    monkeypatch.setattr(app, "get_planner_llm", lambda: _StubLLM())
    monkeypatch.setattr(app, "_stream_plan", _fake_stream_plan)
    monkeypatch.setattr(app, "_extract_doc_fields", _fake_extract)
    return app.build_graph(checkpointer=MemorySaver()), calls


_INPUT = {"query": "帮我生成起诉状", "mode": "assistant", "doc_type": "complaint"}


def test_field_clarify_interrupt_then_filled(monkeypatch):
    """关键缺口 → field_clarify interrupt;回答后重抽补全 → 直达 docx_confirm。"""
    g, calls = _build_graph(
        monkeypatch,
        # 第 1 次(循环前)缺 plaintiff_gender;第 2 次(补答后)已补
        [_FIELD_STUB, {**_FIELD_STUB, "plaintiff_gender": "男"}],
    )
    cfg = {"configurable": {"thread_id": "fc-1"}, "recursion_limit": 40}

    async def run():
        from langgraph.types import Command
        r = await g.ainvoke(_INPUT, config=cfg)
        assert not r.get("final_answer"), "首轮应停在 interrupt"
        snap = await g.aget_state(cfg)
        intr = next(iter(snap.interrupts), None)
        assert intr and intr.value["type"] == "field_clarify"
        assert "原告性别" in intr.value["message"], "缺的关键字段 label 应出现在问题里"
        assert intr.value["round"] == "1/3"
        assert any(p["key"] == "plaintiff_gender" and p["status"] == "pending"
                   for p in intr.value["field_preview"])
        # 回答后重抽取已带 extra_qa → 关键齐 → docx_confirm
        r2 = await g.ainvoke(Command(resume="男"), config=cfg)
        assert not r2.get("final_answer"), "应停在 docx_confirm"
        snap2 = await g.aget_state(cfg)
        assert next(iter(snap2.interrupts), None).value["type"] == "docx_confirm"
        assert calls["n"] == 2, "补答后应重抽取一次"

    asyncio.run(run())


def test_field_clarify_rounds_cap(monkeypatch):
    """关键字段始终缺 → 最多问 max_doc_field_rounds 轮后直入 docx_confirm。"""
    g, calls = _build_graph(monkeypatch, [_FIELD_STUB])  # 每次抽都缺
    cfg = {"configurable": {"thread_id": "fc-2"}, "recursion_limit": 60}

    async def run():
        from langgraph.types import Command
        await g.ainvoke(_INPUT, config=cfg)
        for i in range(3):
            r = await g.ainvoke(Command(resume=f"回答{i}"), config=cfg)
            snap = await g.aget_state(cfg)
            intr = next(iter(snap.interrupts), None)
            if i < 2:
                assert intr and intr.value["type"] == "field_clarify"
                assert intr.value["round"] == f"{i + 2}/3"
            else:
                # 第 3 轮答完 → 轮尽 → docx_confirm
                assert intr and intr.value["type"] == "docx_confirm"
                assert "关键缺失" in intr.value["message"]
        assert calls["n"] >= 4  # 首抽 + 每轮补答后重抽

    asyncio.run(run())


def test_field_clarify_skip_answer_goes_confirm(monkeypatch):
    """空答(跳过补全)→ 不再问, 直接 docx_confirm(带关键缺失提示)。"""
    g, _ = _build_graph(monkeypatch, [_FIELD_STUB])
    cfg = {"configurable": {"thread_id": "fc-3"}, "recursion_limit": 40}

    async def run():
        from langgraph.types import Command
        await g.ainvoke(_INPUT, config=cfg)
        await g.ainvoke(Command(resume=""), config=cfg)
        snap = await g.aget_state(cfg)
        intr = next(iter(snap.interrupts), None)
        assert intr and intr.value["type"] == "docx_confirm"

    asyncio.run(run())


def test_field_clarify_no_critical_gap_no_ask(monkeypatch):
    """无关键缺口 → 不发 field_clarify, 直接 docx_confirm(现状回归)。"""
    full = {k: ("男" if k == "plaintiff_gender" else v) for k, v in _FIELD_STUB.items()}
    g, calls = _build_graph(monkeypatch, [full])
    cfg = {"configurable": {"thread_id": "fc-4"}, "recursion_limit": 40}

    async def run():
        await g.ainvoke(_INPUT, config=cfg)
        snap = await g.aget_state(cfg)
        intr = next(iter(snap.interrupts), None)
        assert intr and intr.value["type"] == "docx_confirm"
        assert calls["n"] == 1

    asyncio.run(run())
```

- [ ] **Step 2: 跑测试确认失败**

Run: `$PY -m pytest tests/test_field_clarify.py -v`
Expected: 新 4 例 FAIL(`KeyError 'doc_field_rounds'` 或停在 docx_confirm 而非 field_clarify),Task 1 的 3 例仍 PASS。

- [ ] **Step 3: 实现**

**(a)** `_extract_doc_fields` 签名与 prompt(替换 `LangGraph_lawApp.py:1166-1209` 对应行):

```python
async def _extract_doc_fields(state: "AgentState", doc_type: str, extra_qa: str = "") -> dict:
```

prompt 内两处改:

```python
    from lawApp_LangGraph.doc_templates import doc_label
    doc_name = doc_label(doc_type)
    qa_block = f"\n\n【补充问答】\n{extra_qa}" if extra_qa else ""
    prompt = f"""你是资深婚姻家事律师助理。从下列案情中为《{doc_name}》抽取字段值。
...(规则/选项参照段原样保留, "诉请依据"字段说明改"答辩的依据"字段按 doc_type: complaint=诉请依据 / defense=答辩的依据, 用 {doc_name} 对应说法)...

【检索法条】
{laws_digest or '无'}{qa_block}
"""
```

**(b)** HITL-4 块:把现有 preview 构建段(`LangGraph_lawApp.py:1320-1340`,即 `fields_defs = ...` 到 `docx_msg = (...)` 之间)替换为:

```python
        fields_defs = {fd["key"]: fd for fd in load_fields(doc_type)}

        def _preview_and_missing(fields: dict):
            preview, crit = [], []
            for k, v in fields.items():
                fd = fields_defs.get(k, {})
                val = str(v or "").strip()
                if val:
                    preview.append({"key": k, "label": fd.get("label", k), "value": val,
                                    "critical": bool(fd.get("critical")), "status": "filled"})
                else:
                    preview.append({"key": k, "label": fd.get("label", k), "value": "待补充",
                                    "critical": bool(fd.get("critical")), "status": "pending"})
                    if fd.get("critical"):
                        crit.append(fd.get("label", k))
            return preview, crit

        # field_clarify: 关键字段缺口循环补全(缺关键再问; 空答/轮尽 → 现状进确认)。
        # interrupt 节点内多次调用, resume 重放已答轮次 — qa_log 在重放中逐轮重建
        from lawApp_LangGraph.doc_templates import doc_label
        doc_name = doc_label(doc_type)
        qa_log = []
        while len(qa_log) < settings.max_doc_field_rounds:
            preview, critical_missing = _preview_and_missing(doc_fields)
            if not critical_missing:
                break
            labels = critical_missing[:5]
            q_text = f"为生成完整《{doc_name}》, 请补充以下关键信息: {'、'.join(labels)}"
            dialogue_log.log_event(
                _dialogue_sid(config),
                "field_question",
                {"round": len(qa_log) + 1, "question": q_text, "fields": labels},
                dedupe_on=("round", "question"),
            )
            answer = interrupt(
                {
                    "type": "field_clarify",
                    "round": f"{len(qa_log) + 1}/{settings.max_doc_field_rounds}",
                    "message": q_text,
                    "field_preview": preview,
                }
            )
            answer = str(answer).strip() if answer else ""
            if not answer:
                break  # 用户跳过补全 → 带现状进确认
            qa_log.append((q_text, answer))
            dialogue_log.log_event(
                _dialogue_sid(config),
                "field_answer",
                {"round": len(qa_log), "question": q_text, "answer": answer},
                dedupe_on=("round", "question"),
            )
            extra_qa = "\n".join(f"问: {q}\n答: {a}" for q, a in qa_log)
            try:
                doc_fields = await _extract_doc_fields(state, doc_type, extra_qa=extra_qa)
            except Exception as e:
                debug.warning("field_clarify 补答后重抽取失败", detail=str(e)[:120])
                break  # 重抽失败 → 用补答前字段进确认

        preview, critical_missing = _preview_and_missing(doc_fields)
        n_filled = sum(1 for p in preview if p["status"] == "filled")
        n_pending = len(preview) - n_filled
        docx_msg = (
            f"即将生成 Word 文书({doc_name})。已填 {n_filled} 项, "
            f"待补充 {n_pending} 项"
            + (f", 关键缺失: {'、'.join(critical_missing[:5])}" if critical_missing else "")
            + "。确认生成吗?"
        )
```

(后续 `interrupt({"type": "docx_confirm", ...})` 起原样保留。)

**(c)** 确认后的直调分支返回(`LangGraph_lawApp.py:1415-1416`)增一个键:

```python
        return {"plan": doing, "messages": [ai_msg], "docx_confirmed": True,
                "doc_fields": confirmed_fields, "doc_field_rounds": len(qa_log)}
```

**(d)** `FastAPI/utils.py` normalize_resume:docstring 的类型列表 `clarify / mid_clarify` 改为 `clarify / mid_clarify / field_clarify`,末行注释同步(`# clarify / mid_clarify / field_clarify: 原文透传`)。**行为零改动** — field_clarify 落到既有"原文透传"默认分支。

**(e)** `InterruptPanel.vue`:

```js
const TYPE_LABEL = {
  // ...(既有 7 类不动)
  field_clarify: '关键信息补充',
}
const TEXT_KINDS = new Set(['clarify', 'mid_clarify', 'field_clarify'])
const PASS_VALUE = {
  // ...(既有不动)
  field_clarify: '',
}
```

跳过按钮文案:模板里 `onPass` 按钮文本沿用既有"跳过"字样即可(全局一个按钮,不加 per-type 文案)。

- [ ] **Step 4: 跑测试确认通过**

Run: `$PY -m pytest tests/test_field_clarify.py tests/test_docx_generation.py -v`
Expected: 全 PASS(test_docx_generation 的 docx_confirm 载荷用例消息含"民事起诉状"字样不受影响 — 如有断言"即将生成 Word 文书(民事起诉状)"旧文案需同步为 `即将生成 Word 文书({doc_name})` 新格式,改动断言属预期)。

- [ ] **Step 5: 提交**

```bash
git add lawApp_LangGraph/LangGraph_lawApp.py lawApp_LangGraph/FastAPI/utils.py frontend/src/components/InterruptPanel.vue tests/test_field_clarify.py
git commit -m "feat: field_clarify 关键字段补全循环 — executor HITL-4 内 interrupt 重放循环(缺关键再问/空答跳过/轮尽兜底), field_question/field_answer 落库 dedupe, 前端面板第8类 interrupt 接线"
```

---

### Task 3: doc_type 通用化去硬编码

**Files:**
- Modify: `lawApp_LangGraph/LangGraph_lawApp.py:1406`(文件名前缀)
- Modify: `lawApp_LangGraph/LangGraph_lawApp.py:812-817`(`_TOOL_DESC_OVERRIDES` generate_docx 文案)
- Modify: `lawApp_LangGraph/prompts.py:394-397`(`PLANNER_ASSISTANT_DOCX_STEP` 文案,如含"起诉状"字样通用化)
- Test: `tests/test_docx_generation.py`(追加)

**Interfaces:**
- Consumes: Task 1 `doc_label`。
- Produces: 文件名 `f"{_DOC_TYPE_FILE_PREFIX[doc_type]}_{safe_sid}.docx"`,前缀映射 `{"complaint": "起诉状", "defense": "答辩状"}`;planner 工具描述"按法院表格模板把案件字段渲染成 Word 文书(起诉状/答辩状)…"。Task 4(defense)依赖此前缀映射。

- [ ] **Step 1: 写失败测试**(追加到 `tests/test_docx_generation.py`,沿用该文件 `_DOCX_T_PREFIX`/`_docx_cleanup` 帮助函数)

```python
def test_docx_filename_prefix_by_doc_type():
    """直调分支文件名前缀按 doc_type: complaint=起诉状 / defense=答辩状。"""
    import lawApp_LangGraph.LangGraph_lawApp as app
    # 从源码级断言太脆 — 直接驱动 executor: 简化为对前缀映射函数的断言
    assert app._DOC_TYPE_FILE_PREFIX["complaint"] == "起诉状"
    assert app._DOC_TYPE_FILE_PREFIX["defense"] == "答辩状"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `$PY -m pytest tests/test_docx_generation.py::test_docx_filename_prefix_by_doc_type -v`
Expected: FAIL — `AttributeError: module ... has no attribute '_DOC_TYPE_FILE_PREFIX'`

- [ ] **Step 3: 实现**

`LangGraph_lawApp.py` 顶部常量区(近 `_TOOL_DESC_OVERRIDES` 处)加:

```python
# assistant 直调分支的文书文件名前缀(doc_type → 中文名)
_DOC_TYPE_FILE_PREFIX = {"complaint": "起诉状", "defense": "答辩状"}
```

executor 直调分支 `:1406` 改:

```python
                "filename": f"{_DOC_TYPE_FILE_PREFIX.get(state.doc_type or 'complaint', '文书')}_{safe_sid}.docx",
```

`_TOOL_DESC_OVERRIDES` 的 generate_docx 文案改:

```python
    "generate_docx": (
        "按法院表格模板把案件字段渲染成 Word 文书(起诉状/答辩状)。"
        '仅在工具清单标注"模板可用"时规划此步骤。'
    ),
```

`prompts.py` `PLANNER_ASSISTANT_DOCX_STEP` 与 `PLANNER_ASSISTANT_SUFFIX` 中如有"起诉状"单一字样,改为"文书(起诉状/答辩状)"通用表述(**只改文案字样,不改结构**)。`FINALIZE_COMPLAINT_PROMPT`/`FINALIZE_DEFENSE_PROMPT` 不动。

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `$PY -m pytest tests/test_docx_generation.py -v` → 全 PASS
Run: `$PY -m pytest tests/ -q` → 全 PASS(文案变更可能影响 planner 相关断言,按需同步)

- [ ] **Step 5: 提交**

```bash
git add lawApp_LangGraph/LangGraph_lawApp.py lawApp_LangGraph/prompts.py tests/test_docx_generation.py
git commit -m "feat: doc_type 通用化 — 文件名前缀映射(起诉状/答辩状), 工具描述与 planner 提示去起诉状单一字样"
```

---

### Task 4: defense 答辩状模板(fields.yaml + build + 测试更新)

**Files:**
- Create: `data/doc_templates/defense/source.docx`(从 `E:\桌面\婚姻案件MD\离婚民事答辩状.docx` 复制)
- Create: `data/doc_templates/defense/fields.yaml`
- Create: `data/doc_templates/defense/template.docx`(脚本产物,一并入库与 complaint 一致)
- Test: `tests/test_docx_generation.py`(改 1 例 + 追加)

**Interfaces:**
- Consumes: `scripts/build_docx_template.py`(`--src/--yaml/--out` 三参,anchor text 模式按"段落可见文本"正则匹配,choice 必须手写 replacement,多行 replacement 逐段落映射/`~` 跳过)。
- Produces: `template_available("defense") == True`;`load_fields("defense")` 约 40 字段(key 约定见 Step 3 YAML);`doc_label("defense") == "民事答辩状"`(fields.yaml 顶层 label,依赖 Task 1)。

- [ ] **Step 1: 复制源表**

```bash
mkdir -p data/doc_templates/defense
cp "E:/桌面/婚姻案件MD/离婚民事答辩状.docx" data/doc_templates/defense/source.docx
```

- [ ] **Step 2: 写 fields.yaml**(完整内容;anchor 逐段对照源表段落文本,paragraph 明细见计划附注)

```yaml
# 离婚民事答辩状 — 字段结构定义(anchor/replacement 语义同 complaint/fields.yaml 头注释)
doc_type: defense
template: template.docx
label: 民事答辩状（离婚纠纷）
fields:
  # ---- 案号/案由 ----
  - {key: case_no, label: 案号, type: text, anchor: '案号', occurrence: 0}
  - {key: case_cause, label: 案由, type: text, anchor: '案由', occurrence: 0}
  # ---- 答辩人 ----
  - {key: respondent_name, label: 答辩人姓名, type: text, critical: true,
     anchor: '姓名：', occurrence: 0}
  - {key: respondent_gender, label: 答辩人性别, type: choice, options: [男, 女], critical: true,
     anchor: '性别：男\s*女', occurrence: 0,
     replacement: '性别：{{ c.respondent_gender_male }}男 {{ c.respondent_gender_female }}女'}
  - {key: respondent_birth_date, label: 答辩人出生日期, type: date,
     anchor: '出生日期：\s*年\s*月\s*日', occurrence: 0,
     replacement: '出生日期：{{ f.respondent_birth_date }}'}
  - {key: respondent_ethnic, label: 答辩人民族, type: text,
     anchor: '民族：', occurrence: 0}
  - {key: respondent_work, label: 答辩人工作单位, type: text,
     anchor: '工作单位：\s*职务：\s*联系电话：', occurrence: 0,
     replacement: '工作单位：{{ f.respondent_work }}  职务：{{ f.respondent_duty }}  联系电话：{{ f.respondent_phone }}'}
  - {key: respondent_duty, label: 答辩人职务, type: text, _merge_into: respondent_work}
  - {key: respondent_phone, label: 答辩人联系电话, type: text, _merge_into: respondent_work}
  - {key: respondent_domicile, label: 答辩人户籍地, type: text,
     anchor: '住所地（户籍所在地）：', occurrence: 0}
  - {key: respondent_residence, label: 答辩人经常居住地, type: text,
     anchor: '经常居住地：', occurrence: 0}
  # ---- 委托诉讼代理人 ----
  - {key: agent_has, label: 有无委托代理人, type: choice, options: [有, 无],
     anchor: '^有\s*$', occurrence: 0,
     replacement: "{{ c.agent_has_yes }}有\n~\n~\n~\n{{ c.agent_has_no }}无"}
  - {key: agent_name, label: 代理人姓名, type: text,
     anchor: '姓名：', occurrence: 1}
  - {key: agent_org, label: 代理人单位, type: text,
     anchor: '(?<!工作)单位：\s*职务：\s*联系电话：', occurrence: 0,
     replacement: '单位：{{ f.agent_org }}  职务：{{ f.agent_duty }}  联系电话：{{ f.agent_phone }}'}
  - {key: agent_duty, label: 代理人职务, type: text, _merge_into: agent_org}
  - {key: agent_phone, label: 代理人电话, type: text, _merge_into: agent_org}
  - {key: agent_scope, label: 代理权限, type: choice, options: [一般授权, 特别授权],
     anchor: '代理权限：一般授权\s*特别授权', occurrence: 0,
     replacement: '代理权限：{{ c.agent_scope_general }}一般授权  {{ c.agent_scope_special }}特别授权'}
  # ---- 送达地址 ----
  - {key: service_addr, label: 送达地址, type: text,
     anchor: '地址：', occurrence: 0}
  - {key: service_receiver, label: 送达收件人, type: text,
     anchor: '收件人：', occurrence: 0}
  - {key: service_receiver_phone, label: 送达收件电话, type: text,
     anchor: '(?<!联系)电话：', occurrence: 0,
     replacement: '电话：{{ f.service_receiver_phone }}'}
  - {key: e_service, label: 是否接受电子送达, type: choice, options: [是, 否],
     anchor: '是\s*方式：', occurrence: 0,
     replacement: "{{ c.e_service_yes }}是  方式：\n{{ c.e_service_no }}否"}
  - {key: e_service_method, label: 电子送达方式, type: choice,
     options: [短信, 微信, 传真, 邮箱, 其他],
     anchor: '短信\s*微信\s*传真\s*邮箱\s*其他', occurrence: 0,
     replacement: '{{ c.e_service_method_sms }}短信  {{ c.e_service_method_wechat }}微信  {{ c.e_service_method_fax }}传真  {{ c.e_service_method_email }}邮箱  {{ c.e_service_method_other }}其他'}
  # ---- 答辩事项 1-7: 每项 确认/异议 choice + 事由 text ----
  - {key: resp_divorce, label: 对解除婚姻关系的态度, type: choice, options: [确认, 异议], critical: true,
     anchor: '1\.对解除婚姻关系的确认和异议', occurrence: 0,
     replacement: '1.对解除婚姻关系的确认和异议\n{{ c.resp_divorce_confirm }}确认      {{ c.resp_divorce_object }}异议'}
  - {key: resp_divorce_reason, label: 解除婚姻关系事由, type: text,
     anchor: '事由：', occurrence: 0,
     replacement: '事由：{{ f.resp_divorce_reason }}'}
  - {key: resp_property, label: 对财产诉请的态度, type: choice, options: [确认, 异议],
     anchor: '2\.对夫妻共同财产诉请的确认和异议', occurrence: 0,
     replacement: '2.对夫妻共同财产诉请的确认和异议\n{{ c.resp_property_confirm }}确认      {{ c.resp_property_object }}异议'}
  - {key: resp_property_reason, label: 财产诉请事由, type: text,
     anchor: '事由：', occurrence: 1,
     replacement: '事由：{{ f.resp_property_reason }}'}
  - {key: resp_debt, label: 对债务诉请的态度, type: choice, options: [确认, 异议],
     anchor: '3\.对夫妻共同债务诉请的确认和异议', occurrence: 0,
     replacement: '3.对夫妻共同债务诉请的确认和异议\n{{ c.resp_debt_confirm }}确认      {{ c.resp_debt_object }}异议'}
  - {key: resp_debt_reason, label: 债务诉请事由, type: text,
     anchor: '事由：', occurrence: 2,
     replacement: '事由：{{ f.resp_debt_reason }}'}
  - {key: resp_custody, label: 对抚养诉请的态度, type: choice, options: [确认, 异议],
     anchor: '4\.对子女直接抚养诉请的确认和异议', occurrence: 0,
     replacement: '4.对子女直接抚养诉请的确认和异议\n{{ c.resp_custody_confirm }}确认      {{ c.resp_custody_object }}异议'}
  - {key: resp_custody_reason, label: 抚养诉请事由, type: text,
     anchor: '事由：', occurrence: 3,
     replacement: '事由：{{ f.resp_custody_reason }}'}
  - {key: resp_alimony, label: 对抚养费诉请的态度, type: choice, options: [确认, 异议],
     anchor: '5\.对子女抚养费诉请的确认和异议', occurrence: 0,
     replacement: '5.对子女抚养费诉请的确认和异议\n{{ c.resp_alimony_confirm }}确认      {{ c.resp_alimony_object }}异议'}
  - {key: resp_alimony_reason, label: 抚养费诉请事由, type: text,
     anchor: '事由：', occurrence: 4,
     replacement: '事由：{{ f.resp_alimony_reason }}'}
  - {key: resp_visit, label: 对探望权诉请的态度, type: choice, options: [确认, 异议],
     anchor: '6\.对子女探望权诉请的确认和异议', occurrence: 0,
     replacement: '6.对子女探望权诉请的确认和异议\n{{ c.resp_visit_confirm }}确认      {{ c.resp_visit_object }}异议'}
  - {key: resp_visit_reason, label: 探望权诉请事由, type: text,
     anchor: '事由：', occurrence: 5,
     replacement: '事由：{{ f.resp_visit_reason }}'}
  - {key: resp_comp, label: 对赔偿/补偿/帮助的态度, type: choice, options: [确认, 异议],
     anchor: '7\.对赔偿/补偿/经济帮助的确认和异议', occurrence: 0,
     replacement: '7.对赔偿/补偿/经济帮助的确认和异议\n{{ c.resp_comp_confirm }}确认      {{ c.resp_comp_object }}异议'}
  - {key: resp_comp_reason, label: 赔偿/补偿/帮助事由, type: text,
     anchor: '事由：', occurrence: 6,
     replacement: '事由：{{ f.resp_comp_reason }}'}
  # ---- 8-10 ----
  - {key: resp_other, label: 其他事由, type: text,
     anchor: '8\.其他事由', occurrence: 0}
  - {key: resp_basis, label: 答辩依据(法条), type: text, critical: true,
     anchor: '9\.答辩的依据', occurrence: 0,
     replacement: '9.答辩的依据\n{{ f.resp_basis }}'}
  - {key: evidence_list, label: 证据清单, type: text,
     anchor: '10\.证据清单（可另附页）', occurrence: 0,
     replacement: '10.证据清单（可另附页）\n{{ f.evidence_list }}'}
  # ---- 落款 ----
  - {key: signer, label: 答辩人签字, type: text,
     anchor: '答辩人（签字、盖章）：', occurrence: 0}
  - {key: sign_date, label: 落款日期, type: date,
     anchor: '(?<!出生)日期：', occurrence: 0}
```

- [ ] **Step 3: 跑 build 脚本生成 template.docx**

```bash
$PY scripts/build_docx_template.py --src data/doc_templates/defense/source.docx --yaml data/doc_templates/defense/fields.yaml --out data/doc_templates/defense/template.docx
```

Expected: 零输出成功;若抛 `ValueError: 字段 xxx 未命中锚点/尾段越界/对账失败`,按报错调 anchor(源表段落实文已核对:`案号 `/`案由`/`性别：男 女`/`有    `/`确认      异议`/`事由：` 等 — 正则空白符 `\s*` 覆盖全角空格不一致时放宽)。anchor 与段落文本的对照清单:

```text
'案号 ' / '案由' / '姓名：'×2 / '性别：男 女' / '出生日期：     年     月    日'
'民族：' / '工作单位：            职务：             联系电话：'
'住所地（户籍所在地）：' / '经常居住地：' / '有    ' / '单位：               职务：              联系电话：'
'代理权限：一般授权  特别授权  ' / '无' / '地址：' / '收件人：' / '电话：'
'是   方式：短信         微信         传真         邮箱          其他        ' / '否'
'1.对解除婚姻关系的确认和异议' / '确认      异议' / '事由：' (×7, 依序 occurrence 0-6)
'2.对夫妻共同财产诉请的确认和异议' … '7.对赔偿/补偿/经济帮助的确认和异议'
'8.其他事由' / '9.答辩的依据' / '法律及司法解释的规定，要写明具体条文' / '10.证据清单（可另附页）'
'附页' / '              答辩人（签字、盖章）：' / '日期： '
```

- [ ] **Step 4: 写/改测试**

改 `test_load_fields_missing_returns_empty`(`tests/test_docx_generation.py:72`,defense 已有模板,断言反转):

```python
def test_load_fields_defense():
    from lawApp_LangGraph.doc_templates import load_fields, template_available
    fields = load_fields("defense")
    assert fields, "defense 模板应可加载"
    keys = {f["key"] for f in fields}
    assert {"respondent_name", "resp_divorce", "resp_basis"} <= keys
    assert template_available("defense"), "defense 模板对账应通过"
```

(原 `test_load_fields_missing_returns_empty` 的"缺目录返回空"语义改用不存在的 doc_type 断言:`load_fields("nonexistent") == []`。)

追加渲染冒烟(沿用该文件 `_run_tool` 帮助函数,`tests/test_docx_generation.py:108-114`):

```python
def test_generate_docx_defense_renders(tmp_path, monkeypatch):
    """defense 模板渲染冒烟: 关键字段落产物, 勾选/待补充兜底同 complaint。"""
    monkeypatch.setenv("DOCX_OUTPUT_DIR", str(tmp_path))
    r = _run_tool(
        {"respondent_name": "王五", "respondent_gender": "男", "resp_divorce": "异议",
         "resp_divorce_reason": "不同意离婚", "resp_basis": "民法典第1079条",
         "signer": "王五", "sign_date": "2026-10-09"},
        doc_type="defense", filename="答辩状_t.docx",
    )
    assert r["status"] == "success" and r["filled"] >= 5
    assert (tmp_path / "答辩状_t.docx").exists()
    text = _doc_text(r["docx_path"])
    assert "王五" in text and "异议" in text
    assert "☑男 ☐女" in text
```

**既有用例联动修正**(同 Step 一并改): `tests/test_docx_generation.py:202` `test_generate_docx_unknown_doctype_error` 现断言 `doc_type="defense"` 报 error,defense 模板上线后失效 — 改为真正不存在的 doc_type:

```python
def test_generate_docx_unknown_doctype_error(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCX_OUTPUT_DIR", str(tmp_path))
    r = _run_tool({"a": "b"}, doc_type="nonexistent")
    assert r["status"] == "error" and r["docx_path"] is None
```

- [ ] **Step 5: 跑测试**

Run: `$PY -m pytest tests/test_docx_generation.py -v`
Expected: 全 PASS(defense 新例 + 反转例)

- [ ] **Step 6: 提交**

```bash
git add data/doc_templates/defense tests/test_docx_generation.py
git commit -m "feat: 答辩状模板上线 — defense/fields.yaml+template.docx(法院源表打标), 40 字段, planner 自动放行 docx 步骤"
```

---

### Task 5: 复杂案情 → 起诉状/答辩状模板渲染端到端测试

**Files:**
- Test: `tests/test_doc_e2e_complex_case.py`(新建,纯测试任务)

**Interfaces:**
- Consumes: Task 4 的 defense 模板 + 既有 complaint 模板;`tests/test_docx_generation.py` 的 `_run_tool`/`_doc_text` 模式(本文件自带同款实现,前缀 `T-doc-e2e`)。
- Produces: 复杂案情 fixture `CASE_TEXT`(自拟完整离婚案情,覆盖双当事人/结婚子女/房车存款/债务/抚养探望/损害赔偿/证据)+ 手工 ground truth 字段映射;为 Task 10 冒烟提供人工校对参照。

- [ ] **Step 1: 写测试**(完整文件;渲染断言不依赖 LLM/PG,唯一可选用例 skipif 无 API key)

```python
"""复杂案情端到端 — 同一案情分别渲染起诉状/答辩状模板, 断言关键字段值/
勾选状态/待补充归一; 可选用例走真实 flash LLM 抽取(无 key 自动跳过)。"""
import json
from pathlib import Path

_DOCX_T_PREFIX = "T-doc-e2e"


def _run_tool(fields, doc_type="complaint", filename=""):
    import asyncio

    from lawApp_LangGraph.tools.tools import generate_docx
    return asyncio.run(generate_docx.ainvoke({
        "fields_json": json.dumps(fields, ensure_ascii=False),
        "doc_type": doc_type, "filename": filename}))


def _doc_text(docx_path) -> str:
    import docx
    d = docx.Document(str(docx_path))
    parts = [p.text for p in d.paragraphs]

    def walk(table):
        for row in table.rows:
            for cell in row.cells:
                parts.extend(p.text for p in cell.paragraphs)
                for nested in cell.tables:
                    walk(nested)

    for t in d.tables:
        walk(t)
    return "\n".join(parts)


# 自拟复杂完整案情(覆盖: 双方身份/婚姻/子女/房产汽车存款/债务/抚养费/探望/
# 损害赔偿/管辖保全/证据; 部分字段故意留白验"待补充"归一)
CASE_TEXT = (
    "原告张三,男,1985年3月12日出生,汉族,户籍北京市朝阳区幸福路10号,现住北京市"
    "海淀区中关村大街8号,联系电话13800000001,在北京华宇科技有限公司任工程师。"
    "被告李四,女,1987年7月25日出生,汉族,户籍河北省石家庄市长安区中山路5号,"
    "现住北京市海淀区中关村大街8号,联系电话13900000002。双方于2008年5月20日在"
    "北京市朝阳区民政局登记结婚,2009年生育长子张小军,2014年生育次女张小丽。"
    "自2019年起被告沉迷赌博屡教不改,双方经常争吵,2021年8月起原告搬至公司宿舍"
    "与被告分居至今,夫妻感情确已破裂。婚姻期间购得北京市海淀区学府路1号房屋一套"
    "(登记双方名下,市值约600万元),别克牌汽车一辆(登记被告名下),招商银行存款"
    "约80万元(原告名下账户)。被告因赌博欠个人债务20万元。两子女出生后一直随"
    "原告及原告父母共同生活,原告请求两子女均由其直接抚养,被告每月支付每名子女"
    "抚养费3000元至年满十八周岁,被告每月可探望子女两次。因被告赌博存在重大过错,"
    "原告请求离婚损害赔偿50000元。双方无仲裁或管辖约定,原告不申请财产保全。"
    "证据: 结婚证、户口簿、分居证明、被告赌博聊天记录、银行流水。"
)

# 案情 → 起诉状 ground truth(手工对出; 未提供字段如民族留给"待补充"断言)
COMPLAINT_FIELDS = {
    "plaintiff_name": "张三", "plaintiff_gender": "男",
    "plaintiff_birth_date": "1985年3月12日", "plaintiff_work": "北京华宇科技有限公司",
    "plaintiff_duty": "工程师", "plaintiff_phone": "13800000001",
    "plaintiff_domicile": "北京市朝阳区幸福路10号",
    "plaintiff_residence": "北京市海淀区中关村大街8号",
    "defendant_name": "李四", "defendant_gender": "女",
    "defendant_birth_date": "1987年7月25日",
    "defendant_domicile": "河北省石家庄市长安区中山路5号",
    "defendant_residence": "北京市海淀区中关村大街8号",
    "defendant_phone": "13900000002",
    "claim_divorce": "因感情确已破裂, 请求判决解除婚姻关系",
    "property_has": "有财产",
    "property_house": "北京市海淀区学府路1号房屋一套(双方名下, 市值约600万元)",
    "property_house_owner": "原告",
    "property_car": "别克牌汽车一辆(被告名下)",
    "property_car_owner": "被告",
    "property_deposit": "招商银行存款约80万元",
    "property_deposit_owner": "原告",
    "debt_has": "有债务",
    "debt1": "被告因赌博欠个人债务20万元",
    "custody_has": "有此问题", "custody_child1": "原告",
    "alimony_has": "有此问题", "alimony_payer": "被告",
    "alimony_amount": "每名子女每月3000元至年满十八周岁",
    "visit_has": "有此问题", "visit_subject": "被告",
    "visit_method": "每月探望子女两次",
    "compensation": "离婚损害赔偿", "comp_damage_amount": "50000元",
    "fact_marriage_time": "2008年5月20日",
    "fact_children": "2009年生长子张小军, 2014年生次女张小丽",
    "fact_divorce_reason": "被告沉迷赌博屡教不改, 2021年8月分居至今",
    "fact_basis": "民法典第一千零七十九条",
    "evidence_list": "结婚证、户口簿、分居证明、赌博聊天记录、银行流水",
    "signer": "张三", "sign_date": "2026年10月9日",
}

# 案情 → 答辩状 ground truth(答辩人=被告李四视角; 7 项态度中仅示意 1/2/4 项,
# 其余缺省验勾选全 ☐ + 待补充归一)
DEFENSE_FIELDS = {
    "case_no": "(2026)京0108民初1234号", "case_cause": "离婚纠纷",
    "respondent_name": "李四", "respondent_gender": "女",
    "respondent_birth_date": "1987年7月25日",
    "respondent_domicile": "河北省石家庄市长安区中山路5号",
    "respondent_residence": "北京市海淀区中关村大街8号",
    "resp_divorce": "异议", "resp_divorce_reason": "不同意离婚, 双方感情尚未破裂",
    "resp_property": "异议",
    "resp_property_reason": "房屋系双方共同财产, 请求依法分割",
    "resp_custody": "异议", "resp_custody_reason": "子女随母亲生活更有利于成长",
    "resp_basis": "民法典第一千零八十四条",
    "evidence_list": "结婚证、户口簿",
    "signer": "李四", "sign_date": "2026年10月9日",
}


def test_complex_case_complaint_render(tmp_path, monkeypatch):
    """复杂案情 → 起诉状: 关键字段值/勾选/关键归一全部落产物。"""
    monkeypatch.setenv("DOCX_OUTPUT_DIR", str(tmp_path))
    r = _run_tool(COMPLAINT_FIELDS, doc_type="complaint", filename="起诉状_e2e.docx")
    assert r["status"] == "success"
    assert (tmp_path / "起诉状_e2e.docx").exists()
    text = _doc_text(r["docx_path"])
    # 当事人与事实主干
    for needle in ("张三", "李四", "13800000001", "北京市海淀区学府路1号",
                   "2008年5月20日", "张小军", "张小丽", "600万元", "3000元",
                   "民法典第一千零七十九条", "赌博聊天记录"):
        assert needle in text, f"起诉状缺关键内容: {needle}"
    # 勾选状态(性别/财产/抚养/探望)
    assert "☑男 ☐女" in text
    assert "☐无财产" in text and "☑有财产" in text
    assert "☑有此问题" in text
    # 事实理由自由文本整段落进产物
    assert "分居至今" in text
    # 案情未给的字段 → 待补充归一(原告民族未提供)
    assert "民族：待补充" in text or "待补充" in text


def test_complex_case_defense_render(tmp_path, monkeypatch):
    """复杂案情 → 答辩状: 答辩人视角字段/确认异议勾选/未答项 ☐ 全落产物。"""
    monkeypatch.setenv("DOCX_OUTPUT_DIR", str(tmp_path))
    r = _run_tool(DEFENSE_FIELDS, doc_type="defense", filename="答辩状_e2e.docx")
    assert r["status"] == "success"
    text = _doc_text(r["docx_path"])
    for needle in ("李四", "离婚纠纷", "(2026)京0108民初1234号",
                   "不同意离婚", "依法分割", "第一千零八十四条"):
        assert needle in text, f"答辩状缺关键内容: {needle}"
    # 1/2/4 项态度勾选: 异议/异议/异议
    assert "☑异议" in text
    # 未表态项(3 债务/5 抚养费/6 探望/7 赔偿)勾选全空
    assert "☐确认" in text
    # 性别勾选: 女
    assert "☐男 ☑女" in text
    # 答辩人未提供的单位/职务 → 待补充
    assert "待补充" in text


def test_extract_doc_fields_from_complex_case():
    """可选端到端: 真实 flash LLM 从复杂案情抽起诉状字段(无 API key 跳过)。"""
    import pytest

    from lawApp_LangGraph.config import settings
    if not settings.deepseek_api_key:
        pytest.skip("无 DEEPSEEK_API_KEY, 跳过真实抽取端到端")
    import asyncio
    import types as _t

    import lawApp_LangGraph.LangGraph_lawApp as app

    st = _t.SimpleNamespace(
        query=CASE_TEXT, user_supplements=[], case_elements=None, law_results=[],
    )
    fields = asyncio.run(app._extract_doc_fields(st, "complaint"))
    assert fields["plaintiff_name"] == "张三"
    assert fields["plaintiff_gender"] == "男"
    assert fields["defendant_name"] == "李四"
    assert fields["fact_marriage_time"].startswith("2008")
    assert "赌博" in fields["fact_divorce_reason"]
```

- [ ] **Step 2: 跑测试**

Run: `$PY -m pytest tests/test_doc_e2e_complex_case.py -v`
Expected: 2 渲染用例 PASS(依赖 Task 4 defense 模板;勾选断言如与模板排版的空格宽窄不符,以 `_doc_text` 实际输出校准空格个数,不放松语义)。

- [ ] **Step 3: 提交**

```bash
git add tests/test_doc_e2e_complex_case.py
git commit -m "test: 复杂案情端到端 — 同一案情渲染起诉状/答辩状双模板, 断言关键字段/勾选/待补充归一 + 可选真实 LLM 抽取用例"
```

---

### Task 6: doc/pdf 双格式下载端点

**Files:**
- Modify: `lawApp_LangGraph/FastAPI/api.py:1080-1128`(改造 `session_docx_latest` 为共用实现 + 新端点)
- Modify: `requirements.txt`(docx2pdf)
- Test: `tests/test_doc_download.py`(新建)

**Interfaces:**
- Consumes: 既有 `docx_generated` 事件 + `DOCX_OUTPUT_DIR` 白名单校验;`PDF_OUTPUT_DIR`(`tools/tools.py:192` 同款 env,默认 `./pdf_outputs`)。
- Produces: `GET /sessions/{session_id}/doc/{format}`,format∈docx|pdf(非法 400);docx 分支返回 `.docx` MIME,pdf 分支 `application/pdf`。旧 `GET /sessions/{sid}/docx/latest` 保留(docx 别名)。Task 7 前端按钮 href 依赖 `/api/sessions/{sid}/doc/{format}`。

- [ ] **Step 1: 装依赖**

```bash
$PY -m pip install docx2pdf
```
`requirements.txt` 追加一行 `docx2pdf`(与现有格式一致,不锁版本则同文件其他行的惯例照抄)。

> 既有用例联动修正(`test_generate_docx_unknown_doctype_error` 改 `doc_type="nonexistent"`)已在 Task 4 完成,本 Task 无需重复。

- [ ] **Step 2: 写失败测试**(新建 `tests/test_doc_download.py`;帮助函数 `_skip_if_no_pg`/`_docx_cleanup`/`_DOCX_T_PREFIX` 从 `tests/test_docx_generation.py:242-300` 原样复制,注意 cleanup 前缀换本文件的 `"T-doc-dl-test"`)

```python
"""doc/{format} 双格式下载端点 — docx 别名等价 / pdf 按需转换+缓存 / 白名单复用。"""
from pathlib import Path

_T_PREFIX = "T-doc-dl-test"


def _mk_docx_event(tmp_path, sid, name="起诉状_test.docx"):
    """落盘假 docx + 注入 docx_generated 事件, 返回文件路径。"""
    from lawApp_LangGraph import dialogue_log

    f = tmp_path / name
    f.write_bytes(b"PK\x03\x04fake-docx-payload")
    dialogue_log.log_event(sid, "docx_generated",
                           {"docx_path": str(f), "filled": 6, "pending": 1})
    return f


def test_docx_alias_equivalent(tmp_path, monkeypatch):
    """/doc/docx 与旧 /docx/latest 返回同一文件。"""
    _skip_if_no_pg()
    _docx_cleanup()
    monkeypatch.setenv("DOCX_OUTPUT_DIR", str(tmp_path))
    sid = _T_PREFIX + "-alias"
    _mk_docx_event(tmp_path, sid)
    try:
        from fastapi.testclient import TestClient

        from lawApp_LangGraph.FastAPI.api import app

        with TestClient(app) as client:
            r1 = client.get(f"/sessions/{sid}/doc/docx")
            r2 = client.get(f"/sessions/{sid}/docx/latest")
            assert r1.status_code == 200 and r2.status_code == 200
            assert r1.content == r2.content == b"PK\x03\x04fake-docx-payload"
    finally:
        _docx_cleanup()


def test_pdf_converted_and_cached(tmp_path, monkeypatch):
    """format=pdf: 转换落 PDF_OUTPUT_DIR + application/pdf; 二次请求命中缓存不重转。"""
    _skip_if_no_pg()
    _docx_cleanup()
    monkeypatch.setenv("DOCX_OUTPUT_DIR", str(tmp_path))
    pdf_dir = tmp_path / "pdfs"
    monkeypatch.setenv("PDF_OUTPUT_DIR", str(pdf_dir))
    sid = _T_PREFIX + "-pdf"
    _mk_docx_event(tmp_path, sid)
    calls = {"n": 0}

    def _fake_convert(src, dst):
        calls["n"] += 1
        Path(dst).write_bytes(b"%PDF-1.4-fake")

    import lawApp_LangGraph.FastAPI.api as api
    monkeypatch.setattr(api, "_convert_docx_pdf", _fake_convert)
    try:
        from fastapi.testclient import TestClient

        from lawApp_LangGraph.FastAPI.api import app

        with TestClient(app) as client:
            r1 = client.get(f"/sessions/{sid}/doc/pdf")
            assert r1.status_code == 200, r1.text
            assert r1.headers["content-type"] == "application/pdf"
            assert (pdf_dir / "起诉状_test.pdf").exists()
            r2 = client.get(f"/sessions/{sid}/doc/pdf")
            assert r2.status_code == 200
        assert calls["n"] == 1, "缓存命中不应二次转换"
    finally:
        _docx_cleanup()


def test_pdf_stale_cache_reconverts(tmp_path, monkeypatch):
    """缓存 pdf mtime 旧于 docx → 重新转换。"""
    _skip_if_no_pg()
    _docx_cleanup()
    monkeypatch.setenv("DOCX_OUTPUT_DIR", str(tmp_path))
    pdf_dir = tmp_path / "pdfs"
    monkeypatch.setenv("PDF_OUTPUT_DIR", str(pdf_dir))
    sid = _T_PREFIX + "-stale"
    f = _mk_docx_event(tmp_path, sid)
    stale = pdf_dir / "起诉状_test.pdf"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"%PDF-old")
    import os
    import time

    time.sleep(0.05)
    os.utime(f, None)  # docx 比缓存新
    calls = {"n": 0}

    def _fake_convert(src, dst):
        calls["n"] += 1
        Path(dst).write_bytes(b"%PDF-new")

    import lawApp_LangGraph.FastAPI.api as api
    monkeypatch.setattr(api, "_convert_docx_pdf", _fake_convert)
    try:
        from fastapi.testclient import TestClient

        from lawApp_LangGraph.FastAPI.api import app

        with TestClient(app) as client:
            r = client.get(f"/sessions/{sid}/doc/pdf")
            assert r.status_code == 200 and r.content == b"%PDF-new"
        assert calls["n"] == 1
    finally:
        _docx_cleanup()


def test_pdf_conversion_failure_502(tmp_path, monkeypatch):
    """转换抛错 → 502, detail 含 Word 提示; docx 下载不受影响。"""
    _skip_if_no_pg()
    _docx_cleanup()
    monkeypatch.setenv("DOCX_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("PDF_OUTPUT_DIR", str(tmp_path / "pdfs"))
    sid = _T_PREFIX + "-502"
    _mk_docx_event(tmp_path, sid)

    def _boom(src, dst):
        raise RuntimeError("COM unavailable")

    import lawApp_LangGraph.FastAPI.api as api
    monkeypatch.setattr(api, "_convert_docx_pdf", _boom)
    try:
        from fastapi.testclient import TestClient

        from lawApp_LangGraph.FastAPI.api import app

        with TestClient(app) as client:
            r = client.get(f"/sessions/{sid}/doc/pdf")
            assert r.status_code == 502
            assert "Word" in r.json()["detail"]
            assert client.get(f"/sessions/{sid}/doc/docx").status_code == 200
    finally:
        _docx_cleanup()


def test_doc_format_invalid_400(tmp_path, monkeypatch):
    """format=exe → 400 invalid_format(不触文件查找)。"""
    monkeypatch.setenv("DOCX_OUTPUT_DIR", str(tmp_path))
    from fastapi.testclient import TestClient

    from lawApp_LangGraph.FastAPI.api import app

    with TestClient(app) as client:
        r = client.get(f"/sessions/AT-20990101-000000-999/doc/exe")
        assert r.status_code == 400
        assert r.json()["detail"] == "invalid_format"
```

(文件头帮助函数从 `tests/test_docx_generation.py:276-300` 原样复制 `_skip_if_no_pg`/`_docx_cleanup`,事件清理前缀参数换 `"T-doc-dl-test"`;上面测试引用的 `_convert_docx_pdf`/`_docx_to_pdf` 见 Step 4 实现)

- [ ] **Step 3: 跑测试确认失败**

Run: `$PY -m pytest tests/test_doc_download.py -v`
Expected: FAIL — 404 路由不存在(`/doc/docx`)或 400

- [ ] **Step 4: 实现**(api.py;`session_docx_latest` 主体抽出为内部函数,两个路由共用)

```python
async def _session_doc_path(session_id: str) -> str:
    """会话最新 docx 路径(事件查询 + DOCX_OUTPUT_DIR 白名单); 无 → 抛 404。"""
    # = 现 session_docx_latest :1095-1120 的校验/查询逻辑原样搬入, 返回 path
    ...


def _convert_docx_pdf(docx_path: str, pdf_path: str) -> None:
    """同步 docx→pdf(Word COM); 模块级便于测试 monkeypatch。"""
    from docx2pdf import convert
    convert(docx_path, pdf_path)


async def _docx_to_pdf(docx_path: str) -> str:
    """docx → pdf(Word COM, 30s 超时); 输出 PDF_OUTPUT_DIR 同名 .pdf, mtime 缓存。"""
    import asyncio as _aio

    pdf_dir = os.path.abspath(os.getenv("PDF_OUTPUT_DIR", "./pdf_outputs"))
    os.makedirs(pdf_dir, exist_ok=True)
    pdf_path = os.path.join(pdf_dir, os.path.splitext(os.path.basename(docx_path))[0] + ".pdf")
    if os.path.exists(pdf_path) and os.path.getmtime(pdf_path) >= os.path.getmtime(docx_path):
        return pdf_path
    try:
        await _aio.wait_for(_aio.to_thread(_convert_docx_pdf, docx_path, pdf_path), timeout=30)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"PDF 转换失败(需本机安装 Microsoft Word): {e}")
    return pdf_path


@app.get("/sessions/{session_id}/doc/{format}")
async def session_doc(session_id: str, format: str):
    """双格式文书下载: docx=原文件, pdf=Word COM 转换(mtime 缓存)。"""
    if format not in ("docx", "pdf"):
        raise HTTPException(status_code=400, detail="invalid_format")
    path = await _session_doc_path(session_id)
    if format == "docx":
        return FileResponse(path=path, filename=os.path.basename(path),
            media_type="application/vnd.openxmlformats-officedocument"
                       ".wordprocessingml.document")
    pdf = await _docx_to_pdf(path)
    return FileResponse(path=pdf, filename=os.path.basename(pdf),
                        media_type="application/pdf")


@app.get("/sessions/{session_id}/docx/latest")
async def session_docx_latest(session_id: str):
    """旧路径兼容别名 → docx 分支(契约不变)。"""
    return await session_doc(session_id, "docx")
```

- [ ] **Step 5: 跑测试确认通过**

Run: `$PY -m pytest tests/test_doc_download.py tests/test_docx_generation.py -v`
Expected: 全 PASS(旧端点三态 404/穿越/文件缺失用例经别名继续成立)

- [ ] **Step 6: 提交**

```bash
git add lawApp_LangGraph/FastAPI/api.py requirements.txt tests/test_doc_download.py
git commit -m "feat: /sessions/{sid}/doc/{format} 双格式下载 — docx2pdf(Word COM) 30s 超时 + mtime 缓存, 旧 docx/latest 端点保留为别名"
```

---

### Task 7: 前端 DocReadyModal(模态双下载,替换 toast)

**Files:**
- Create: `frontend/src/components/DocReadyModal.vue`
- Modify: `frontend/src/store.js:29-30,59-67`(docxToast → showDocReady)
- Modify: `frontend/src/views/ChatPage.vue:109-113`(docx_done 帧)`:285-286`(挂载)
- Modify: `frontend/src/components/ChatView.vue:111-119`(终答区加 PDF 按钮)
- Delete: `frontend/src/components/DocxDoneToast.vue`

**Interfaces:**
- Consumes: Task 6 的 `/api/sessions/{sid}/doc/{format}`;SSE `docx_done` 帧载荷 `{path: str}`。
- Produces: `state.showDocReady: boolean`(resetTurn 复位);模态根 id `doc-ready-modal`,按钮 id `btn-download-docx-modal` / `btn-download-pdf-modal` / `btn-doc-ready-close`。

- [ ] **Step 1: store.js**

`state` 里 `docxToast: false` 行改 `showDocReady: false`(注释:docx 完成模态弹窗);`resetTurn` 的 `docxToast: false` 同步改 `showDocReady: false`;`COLLAPSE_LEN` 及其余不动。`docxPath`/`docxGenerating` 保留。

- [ ] **Step 2: DocReadyModal.vue**

```vue
<script setup>
import { state } from '../store'
// 文书生成完成模态: 双格式下载(Word 走原链路, PDF 走 Task 5 端点)
const fileUrl = (f) => '/api/sessions/' + encodeURIComponent(state.value.sessionId) + '/doc/' + f
const name = () => (state.value.docxPath || '').split(/[\\/]/).pop() || 'Word 文书'
function close() { state.value.showDocReady = false }
</script>
<template>
  <div v-if="state.showDocReady" id="doc-ready-modal"
       class="fixed inset-0 z-50 flex items-center justify-center bg-black/30"
       @click.self="close">
    <div class="doc-ready-card w-80 rounded-xl bg-white shadow-xl p-5 text-sm">
      <div class="font-semibold text-slate-800 mb-1">文书已生成</div>
      <div class="text-slate-500 mb-1 truncate" :title="name()">{{ name() }}</div>
      <div class="text-[10px] text-slate-400 mb-4">内容由 AI 生成, 需执业律师复核后使用</div>
      <div class="flex gap-2">
        <a :id="'btn-download-docx-modal'" :href="fileUrl('docx')" download
           class="btn flex-1 rounded-lg bg-amber-600 text-white py-1.5 text-center"
           @click="close">下载 Word</a>
        <a :id="'btn-download-pdf-modal'" :href="fileUrl('pdf')" download
           class="btn flex-1 rounded-lg bg-slate-700 text-white py-1.5 text-center"
           @click="close">下载 PDF</a>
      </div>
      <button id="btn-doc-ready-close"
              class="mt-3 w-full text-xs text-slate-400 underline"
              @click="close">关闭</button>
    </div>
  </div>
</template>
```

- [ ] **Step 3: ChatPage.vue 接线**

事件路由 `docx_done` 分支(`:109-113`)改:

```js
  } else if (e.event === 'docx_done') {
    // docx 生成完成帧: 收生成中弹窗, 开双格式下载模态, 记路径供终答区按钮
    state.value.docxGenerating = false
    state.value.showDocReady = true
    state.value.docxPath = (e.data && e.data.path) || ''
  }
```

挂载区(`:285-286`)`<DocxDoneToast />` 换 `<DocReadyModal />`;import 同步换。删除 `frontend/src/components/DocxDoneToast.vue` 文件。

- [ ] **Step 4: ChatView.vue 终答区**

`下载 Word 文书` 按钮后并列加:

```html
      <a
        id="btn-download-pdf"
        class="inline-block px-4 py-1.5 rounded-lg bg-slate-700 text-white text-sm ml-2"
        :href="'/api/sessions/' + encodeURIComponent(state.sessionId) + '/doc/pdf'"
        download
      >下载 PDF</a>
```

- [ ] **Step 5: 构建校验**

Run: `cd frontend && npm run build`
Expected: 零错误;`grep -rn "DocxDoneToast" frontend/src` 零命中。

- [ ] **Step 6: 提交**

```bash
git add frontend/src/components/DocReadyModal.vue frontend/src/store.js frontend/src/views/ChatPage.vue frontend/src/components/ChatView.vue
git rm frontend/src/components/DocxDoneToast.vue
git commit -m "feat: 文书完成改模态弹窗双格式下载 — DocReadyModal 替换 8s toast, 终答区加 PDF 按钮"
```

---

### Task 8: 案情长文折叠改模糊渐变

**Files:**
- Modify: `frontend/src/components/ChatView.vue:80-92`(用户气泡折叠渲染)

**Interfaces:**
- Consumes: 既有 `COLLAPSE_LEN`(store.js)/`m.collapsed`/`m.expanded`。
- Produces: 折叠态为限高渐变隐没(不再是 `slice(0,120)+'…'`),DOM id `user-msg-collapsed-` 前缀容器。

- [ ] **Step 1: 实现渲染改造**(模板替换 `:81-92` 的用户气泡分支)

```html
          <!-- 用户消息: 超长默认折叠(限高+底部渐变隐没), 点击展开 -->
          <template v-else-if="m.role === 'user'">
            <span
              v-if="m.collapsed && !m.expanded"
              class="user-msg-collapsed relative block max-h-24 overflow-hidden whitespace-pre-wrap"
              :style="{
                '-webkit-mask-image': 'linear-gradient(to bottom, black 55%, transparent 100%)',
                'mask-image': 'linear-gradient(to bottom, black 55%, transparent 100%)',
              }"
            >{{ m.text }}</span>
            <span v-else class="whitespace-pre-wrap">{{ m.text }}</span>
            <button
              v-if="m.collapsed"
              class="block mt-1 text-xs underline opacity-70"
              @click="m.expanded = !m.expanded"
            >
              {{ m.expanded ? '收起' : '展开全文' }}
            </button>
          </template>
```

要点:`slice(0, COLLAPSE_LEN)` 硬截断删除;`max-h-24`(96px,约 3-4 行)限高;mask 渐变在容器上做模糊隐没(不需要 JS 测量)。`COLLAPSE_LEN` 仍由 submit 处 `text.length > COLLAPSE_LEN` 决定是否折叠,不动。

- [ ] **Step 2: 构建校验**

Run: `cd frontend && npm run build`
Expected: 零错误(COLLAPSE_LEN import 若因此不再被 ChatView 使用,`grep -n COLLAPSE_LEN frontend/src` 检查 — store.js 定义与 ChatPage.vue submit 处仍引用,import 只在 ChatView 不再需要时移除该行 import)。

- [ ] **Step 3: 提交**

```bash
git add frontend/src/components/ChatView.vue
git commit -m "feat: 用户气泡长文折叠改限高+mask 渐变隐没(替换硬截断+省略号), 阈值沿 COLLAPSE_LEN"
```

---

### Task 9: 监控页触顶列改运行时长列

**Files:**
- Modify: `frontend/src/views/MonitorView.vue:166-190`(表头+行)与 script 区(加 fmtDuration)

**Interfaces:**
- Consumes: `r.metrics.total_latency_ms`(数值, ms)、`r.metrics.limit_hit`(布尔)。
- Produces: "运行时长"列(如 `45s`/`3.2m`),limit_hit 行红色 + title="触顶运行"。

- [ ] **Step 1: script 区加帮助函数**(放 `scoreCls` 旁)

```js
// 运行时长格式化: <60s 显秒, 否则显分钟(1 位小数); 缺失 → null(模板显 —)
function fmtDuration(ms) {
  if (ms == null || isNaN(ms) || ms < 0) return null
  if (ms < 60000) return `${Math.round(ms / 1000)}s`
  return `${(ms / 60000).toFixed(1)}m`
}
```

- [ ] **Step 2: 模板改**

表头(`:166`)`<th>触顶</th>` 改 `<th>运行时长</th>`;行(`:189-190`)触顶格替换:

```html
            <td :class="r.metrics?.limit_hit ? 'text-red-500' : 'text-slate-400'"
                :title="r.metrics?.limit_hit ? '触顶运行' : ''">
              {{ fmtDuration(r.metrics?.total_latency_ms) ?? '—' }}
            </td>
```

总览卡"触顶运行(limit_hit)"计数(`:121-123`)不动。

- [ ] **Step 3: 构建校验**

Run: `cd frontend && npm run build`
Expected: 零错误

- [ ] **Step 4: 提交**

```bash
git add frontend/src/views/MonitorView.vue
git commit -m "feat: 监控运行列表触顶列改运行时长列(total_latency_ms 格式化, 触顶行红色+title 提示)"
```

---

### Task 10: 全量回归 + 浏览器冒烟

**Files:** 无新文件(验证任务)。

- [ ] **Step 1: 后端全量**

Run: `$PY -m pytest tests/ -q`
Expected: 0 FAIL(基线 197 + 本期新增用例;失败逐个修复,不改断言放水)

- [ ] **Step 2: 前端构建**

Run: `cd frontend && npm run build`
Expected: 零错误零警告

- [ ] **Step 3: 起 服务 冒烟**(浏览器手工/脚本)

```bash
# 后端(注意 8200, 理由见 Global Constraints)
$PY -m uvicorn lawApp_LangGraph.FastAPI.api:app --host 127.0.0.1 --port 8200 --loop lawApp_LangGraph.FastAPI.loop:selector_loop_factory
$PY -m lawApp_LangGraph.mcp.mcp_server   # law-search MCP(另终端)
cd frontend && BACKEND_PORT=8200 npm run dev
```

冒烟清单(浏览器 http://localhost:5173):
1. 切"律师助理"模式,选"答辩状",粘贴长案情 → 问诊要素循环 → field_clarify 关键信息补充面板(文本输入+跳过)→ docx_confirm 预览(《民事答辩状》文案)→ 确认 → DocReadyModal 弹窗 → 下载 Word(答辩状文件名)/ 下载 PDF 均落浏览器下载目录。
2. 起诉状全链路回归(确认无 doc_label/文件名回归)。
3. 粘贴 >120 字案情 → 用户气泡折叠渐变 → 展开全文/收起。
4. 监控页 → 运行列表"运行时长"列显示, 触顶行红字。
5. 监控页新增运行的 trace 里 field_clarify 轮次可见(sessions/{sid}/dialogue 事件含 field_question/field_answer)。

- [ ] **Step 4: 提交(若有冒烟修复)**

```bash
git add -A && git commit -m "fix: 冒烟修复(按实际问题描述)"
```

---

## 附注: 执行顺序依赖

Task 1 → 2 → 3 → 4 → 5(后端链, 顺序执行);Task 6(PDF 端点)在 4 后;Task 7(模态)依赖 6(URL);Task 8/9(折叠/监控)独立;Task 10 收尾。前端三项(7/8/9)互相独立。
