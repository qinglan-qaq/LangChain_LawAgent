"""HITL 单问聚焦批次(每轮 1 个反问 + 推荐选项 + 会话 id 时间戳格式)回归测试。

不依赖真 PG / DeepSeek / MCP server —— 全部 stub/monkeypatch(参照
tests/test_boundary_high_backend.py 的替身模式)。覆盖:
    1. ask_element 单问载荷: 只取 pending_questions[0], options 以
       A/B/C 字母标注 + allow_other=True
    2. options 为空 → 载荷不带 options / allow_other 键(向后兼容)
    3. resume 记录 ClarifyExchange(含 options)进 clarify_history
    4. mid_clarify 载荷 options 字母标注 + allow_other
    5. new_session_id 模式-时间-编号格式 + 同秒编号不撞;
       ensure_session 兼容 legacy 时间戳与 uuid 格式
    6. build_response 暴露 clarify_history
    7. Settings 默认 max_clarify_rounds == 5
"""
import asyncio
import re
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


#  模块级全局清理(防单例/计数器污染串测试)
@pytest.fixture(autouse=True)
def _reset_module_globals():
    yield
    import lawApp_LangGraph.FastAPI.api as api
    import lawApp_LangGraph.FastAPI.utils as fastapi_utils
    import lawApp_LangGraph.LangGraph_lawApp as app

    api._SESSION_LOCKS.clear()
    app._REASONING_BUS.clear()
    fastapi_utils._SID_COUNTER.clear()


def _run(coro):
    return asyncio.run(coro)


# ── 1/2/3: ask_element 单问载荷 + resume 记账 ──


def _patch_interrupt(monkeypatch, resume_value):
    """替换模块级 interrupt, 捕获载荷并返回固定 resume 值。"""
    import lawApp_LangGraph.LangGraph_lawApp as app

    captured = {}

    def fake_interrupt(payload):
        captured["payload"] = payload
        return resume_value

    monkeypatch.setattr(app, "interrupt", fake_interrupt, raising=True)
    return captured


def test_ask_element_single_question_payload_with_options(monkeypatch):
    """多问题在列也只问第 1 个; options 以 A/B/C 字母标注 + allow_other。"""
    import lawApp_LangGraph.LangGraph_lawApp as app
    from lawApp_LangGraph.state import AgentState, ElementQuestion

    captured = _patch_interrupt(monkeypatch, "结婚5年")

    state = AgentState(
        query="我想离婚",
        pending_questions=[
            ElementQuestion(
                key="marriage_status",
                question="请问结婚多少年了?",
                options=["不到3年", "3到10年", "10年以上"],
            ),
            ElementQuestion(key="demand", question="您的核心诉求是什么?"),
        ],
    )
    out = app.ask_element_node(state)

    p = captured["payload"]
    assert p["type"] == "clarify"
    # 只取第 1 个问题(不再把全部 pending 拼成一句)
    assert p["question"] == "请问结婚多少年了?"
    assert p["round"] == "1/5"
    assert p["options"] == [
        {"value": "A", "label": "不到3年"},
        {"value": "B", "label": "3到10年"},
        {"value": "C", "label": "10年以上"},
    ]
    assert p["allow_other"] is True
    assert p["elements"], "要素面板数据必须保留"
    # 只记第 1 个问题的要素 key
    ex = out["clarify_history"][0]
    assert ex.element_keys == ["marriage_status"]
    assert ex.question == "请问结婚多少年了?"


def test_ask_element_empty_options_omits_keys(monkeypatch):
    """options 为空 → 载荷不带 options/allow_other 键(向后兼容旧消费方)。"""
    import lawApp_LangGraph.LangGraph_lawApp as app
    from lawApp_LangGraph.state import AgentState, ElementQuestion

    captured = _patch_interrupt(monkeypatch, "结婚5年")

    state = AgentState(
        query="我想离婚",
        pending_questions=[
            ElementQuestion(key="marriage_status", question="请问结婚多少年了?")
        ],
    )
    app.ask_element_node(state)

    p = captured["payload"]
    assert p["question"] == "请问结婚多少年了?"
    assert "options" not in p, "无推荐选项时不允许出现 options 键"
    assert "allow_other" not in p, "无推荐选项时不允许出现 allow_other 键"


def test_ask_element_resume_records_options_in_clarify_history(monkeypatch):
    """resume 后 clarify_history 记录 ClarifyExchange(含 options)、轮数自增、补充入 user_supplements。"""
    import lawApp_LangGraph.LangGraph_lawApp as app
    from lawApp_LangGraph.state import AgentState, ClarifyExchange, ElementQuestion

    captured = _patch_interrupt(monkeypatch, "结婚5年")

    state = AgentState(
        query="我想离婚",
        clarify_rounds=0,
        pending_questions=[
            ElementQuestion(
                key="marriage_status",
                question="请问结婚多少年了?",
                options=["不到3年", "3到10年"],
            )
        ],
    )
    out = app.ask_element_node(state)

    ex = out["clarify_history"][0]
    assert isinstance(ex, ClarifyExchange)
    assert ex.round == 1
    assert ex.answer == "结婚5年"
    assert ex.options == ["不到3年", "3到10年"], "ClarifyExchange 必须带本轮反问的选项"
    assert out["clarify_rounds"] == 1
    assert out["user_supplements"] == ["结婚5年"]
    assert captured["payload"]["options"][0] == {"value": "A", "label": "不到3年"}


def test_ask_element_no_questions_returns_empty():
    """无 pending_questions → 空 dict, 不 interrupt(resume 重跑幂等)。"""
    import lawApp_LangGraph.LangGraph_lawApp as app
    from lawApp_LangGraph.state import AgentState

    assert app.ask_element_node(AgentState(query="我想离婚")) == {}


# ── 4: mid_clarify 载荷选项 ──


def _patch_mid_clarify_llm(monkeypatch, verdict):
    """打桩 mid_clarify_node 的结构化输出链(PromptTemplate | _structured)。"""
    import lawApp_LangGraph.LangGraph_lawApp as app

    class _FakeChain:
        async def ainvoke(self, _inp, config=None, **_kw):
            return verdict

    class _FakePrompt:
        @classmethod
        def from_template(cls, _template):
            return cls()

        def __or__(self, _other):
            return _FakeChain()

    async def _noop_publish(_config, _node):
        return None

    monkeypatch.setattr(app, "PromptTemplate", _FakePrompt)
    monkeypatch.setattr(app, "_structured", lambda schema: None)
    monkeypatch.setattr(app, "_publish_node_status", _noop_publish)


def test_mid_clarify_payload_options_letters_and_allow_other(monkeypatch):
    """mid_clarify: options 非空 → A/B 字母标注 + allow_other; 补充进 user_supplements。"""
    import lawApp_LangGraph.LangGraph_lawApp as app
    from lawApp_LangGraph.state import AgentState

    verdict = types.SimpleNamespace(
        question="你的房子是婚前买的还是婚后买的?",
        element_key="property",
        options=["婚前全款买的", "婚后共同还贷"],
    )
    _patch_mid_clarify_llm(monkeypatch, verdict)

    captured = _patch_interrupt(monkeypatch, "婚前买的")

    state = AgentState(query="婚前房产归属", rag_documents=[])
    out = _run(app.mid_clarify_node(state, None))

    p = captured["payload"]
    assert p["type"] == "mid_clarify"
    assert p["question"] == "你的房子是婚前买的还是婚后买的?"
    assert p["options"] == [
        {"value": "A", "label": "婚前全款买的"},
        {"value": "B", "label": "婚后共同还贷"},
    ]
    assert p["allow_other"] is True
    assert out["user_supplements"] == [
        "[检索反馈追问] 你的房子是婚前买的还是婚后买的?\n[用户澄清] 婚前买的"
    ]


def test_mid_clarify_empty_options_omits_keys(monkeypatch):
    """mid_clarify: options 为空 → 载荷不带 options/allow_other 键。"""
    import lawApp_LangGraph.LangGraph_lawApp as app
    from lawApp_LangGraph.state import AgentState

    verdict = types.SimpleNamespace(
        question="对方对离婚的态度如何?", element_key="opposing_stance", options=[]
    )
    _patch_mid_clarify_llm(monkeypatch, verdict)
    captured = _patch_interrupt(monkeypatch, "坚决不离")

    _run(app.mid_clarify_node(AgentState(query="离婚", rag_documents=[]), None))

    p = captured["payload"]
    assert "options" not in p
    assert "allow_other" not in p


# ── 5: 会话 id 模式-时间-编号 格式 ──


def test_new_session_id_timestamp_format_and_same_second_uniqueness():
    from lawApp_LangGraph.FastAPI.utils import new_session_id

    pat = re.compile(r"^(AT|AS)-\d{8}-\d{6}-\d{3}$")
    for _ in range(20):
        assert pat.match(new_session_id("attorney")), "格式必须为 模式-时间-编号(3位)"
    assert new_session_id("assistant").startswith("AS-")
    # 同秒两次生成 → 编号不同(进程内锁+计数)
    assert new_session_id("attorney") != new_session_id("attorney")


def test_ensure_session_accepts_legacy_and_uuid():
    from lawApp_LangGraph.FastAPI.utils import ensure_session

    # legacy 时间戳格式(新格式与其同形, 校验规则不变)
    assert ensure_session("AT-20260918-143025-001", "attorney") == "AT-20260918-143025-001"
    # 存量 uuid 格式(仍可恢复)
    assert ensure_session("AT-1a2b3c4d5e6f", "attorney") == "AT-1a2b3c4d5e6f"


# ── 6: build_response 暴露 clarify_history ──


def test_build_response_includes_clarify_history():
    from lawApp_LangGraph.FastAPI.utils import build_response
    from lawApp_LangGraph.state import ClarifyExchange

    state = {
        "query": "我想离婚",
        "final_answer": "回答",
        "clarify_history": [
            ClarifyExchange(
                round=1,
                question="请问结婚多少年了?",
                answer="结婚5年",
                element_keys=["marriage_status"],
                options=["不到3年", "3到10年", "10年以上"],
            ),
            # 旧 checkpoint 形状(无 options)也能取全字段
            {
                "round": 2,
                "question": "核心诉求是什么?",
                "answer": "只想离婚",
                "element_keys": ["demand"],
                "at": "2026-09-21T10:00:00",
            },
        ],
    }
    resp = build_response(state, "T-CLF-1")
    assert len(resp.clarify_history) == 2
    first, second = resp.clarify_history
    assert first["round"] == 1
    assert first["question"] == "请问结婚多少年了?"
    assert first["answer"] == "结婚5年"
    assert first["options"] == ["不到3年", "3到10年", "10年以上"]
    assert second["options"] == [], "缺 options 的旧记录兜底为空列表"
    assert second["at"] == "2026-09-21T10:00:00"


# ── 7: 澄清轮数上限默认值 ──


def test_settings_default_max_clarify_rounds():
    """默认 max_clarify_rounds == 5(_env_file=None 屏蔽 .env, 读纯默认值)。"""
    from lawApp_LangGraph.config import Settings

    assert Settings(_env_file=None).max_clarify_rounds == 5
