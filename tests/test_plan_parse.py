"""planner JSON 解析 — 合法输出 / 模型畸形修复 / 失败必须 raise。"""
from __future__ import annotations

import json

import pytest


def _plan_json() -> str:
    return json.dumps(
        {
            "reasoning": ["先判断是否需要检索。", "再评估案例质量。"],
            "plan": [
                {
                    "step_id": 1,
                    "description": "检索相关判例",
                    "tool_name": "retrieve_legal_knowledge",
                    "args": {"query": "离婚 抚养权"},
                }
            ],
        },
        ensure_ascii=False,
    )


def test_parse_plan_json_accepts_valid_content():
    from lawApp_LangGraph.LangGraph_lawApp import _parse_plan_json

    result = _parse_plan_json(_plan_json())
    assert result.plan[0].tool_name == "retrieve_legal_knowledge"
    assert result.plan[0].args == {"query": "离婚 抚养权"}
    assert result.reasoning == ["先判断是否需要检索。", "再评估案例质量。"]


def test_parse_plan_json_repairs_stray_quote_after_reasoning_array():
    """线上真实畸形: `reasoning` 数组的 `]` 后多一个引号。"""
    from lawApp_LangGraph.LangGraph_lawApp import _parse_plan_json

    raw = (
        '{"reasoning": ["先判断是否需要检索。", "再评估案例质量。"]", '
        '"plan": [{"step_id": 1, "description": "检索相关判例", '
        '"tool_name": "retrieve_legal_knowledge"}]}'
    )
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)

    result = _parse_plan_json(raw)
    assert len(result.plan) == 1
    assert result.plan[0].tool_name == "retrieve_legal_knowledge"


def test_parse_plan_json_repairs_quote_after_nested_object():
    from lawApp_LangGraph.LangGraph_lawApp import _parse_plan_json

    raw = '{"reasoning": [], "plan": [], "meta": {"a": 1}"}'
    result = _parse_plan_json(raw)
    assert result.plan == []


def test_parse_plan_json_keeps_quotes_inside_strings():
    """字符串内部的 `]\"` 不能被当成畸形删除。"""
    from lawApp_LangGraph.LangGraph_lawApp import _parse_plan_json

    raw = '{"reasoning": ["数组以]\\"结尾"], "plan": []}'
    result = _parse_plan_json(raw)
    assert result.reasoning == ['数组以]"结尾']


def test_parse_plan_json_strips_code_fence_and_trailing_prose():
    from lawApp_LangGraph.LangGraph_lawApp import _parse_plan_json

    raw = f"```json\n{_plan_json()}\n```\n以上是计划, 请执行。"
    result = _parse_plan_json(raw)
    assert len(result.plan) == 1


def test_parse_plan_json_ignores_trailing_object_example():
    """raw_decode 只取第一个完整对象, 不吞尾部示例。"""
    from lawApp_LangGraph.LangGraph_lawApp import _parse_plan_json

    raw = _plan_json() + '\n示例: {"reasoning": [], "plan": []}'
    result = _parse_plan_json(raw)
    assert result.plan[0].step_id == 1


def test_parse_plan_json_raises_clear_error_on_truncated_json():
    from lawApp_LangGraph.LangGraph_lawApp import _parse_plan_json

    raw = '{"reasoning": ["还没写完", "plan": ['
    with pytest.raises(ValueError, match="Planner JSON 解析失败"):
        _parse_plan_json(raw)


def test_parse_plan_json_raises_when_no_object():
    from lawApp_LangGraph.LangGraph_lawApp import _parse_plan_json

    with pytest.raises(ValueError, match="未找到 JSON 对象"):
        _parse_plan_json("这不是 JSON")
