"""field_clarify 字段补全循环 — 状态/配置/循环/落库 全链测试。"""

import asyncio
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
    """label 取 fields.yaml 顶层 label; 缺模板/缺键兜底「Word 文书」。

    complaint label 为全称(含案件类型后缀, 实际 YAML 值);
    defense 模板 Task 4 才上线: 上线前无模板走兜底, 上线后读 YAML 顶层
    label(计划 T4 YAML 为全称), 故两态均断言通过, 防跨任务状态翻转。
    """
    from lawApp_LangGraph.doc_templates import doc_label
    assert doc_label("complaint") == "民事起诉状（离婚纠纷）"
    assert doc_label("defense") in (
        "Word 文书", "民事答辩状", "民事答辩状（离婚纠纷）",
    )
    assert doc_label("unknown") == "Word 文书"


# ---- Task 2: executor 补全循环(图内流, LLM 全替身) ----

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

    calls = {"n": 0, "extra": []}  # extra: 每次抽取调用收到的 extra_qa 原文

    async def _fake_extract(state, doc_type, extra_qa=""):
        f = fields_seq[min(calls["n"], len(fields_seq) - 1)]
        calls["n"] += 1
        calls["extra"].append(extra_qa)
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
        # 抽取调用序列: 首抽缺 plaintiff_gender; resume 后节点从头重跑,
        # 重放首抽(无 extra_qa)仍缺, 补答后重抽(带 extra_qa)已补
        # (langgraph 按 interrupt 调用序匹配 resume 值: 重放必须先重放
        # field_clarify 轮, 才不会让 docx_confirm 误消费补答值)
        [_FIELD_STUB, _FIELD_STUB, {**_FIELD_STUB, "plaintiff_gender": "男"}],
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
        # 重放首抽(1) + 首抽(1) + 补答后重抽取(1) = 3 次
        assert calls["n"] == 3, "补答后应带 extra_qa 重抽取一次"
        # 补答后重抽取须携带问答上下文(【补充问答】块原料)
        assert "问: " in calls["extra"][2] and "答: 男" in calls["extra"][2]
        assert calls["extra"][0] == "" and calls["extra"][1] == ""

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
