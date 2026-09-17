"""工程地基测试 — 配置中心 / JSON 日志 / 死字段清理 / 递归限制。

运行: python -m pytest tests/ -q
设计来源: docs/superpowers/specs/2026-09-11-engineering-foundation-design.md
"""
from __future__ import annotations

from pathlib import Path

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
    # 数据文件默认指向仓库 data/, 用 __file__ 计算
    data_dir = Path(__file__).resolve().parents[1] / "data"
    assert s.bm25_path == str(data_dir / "bm25_law_params.json")
    assert s.documents_dir == str(data_dir / "Documents")
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


def test_graph_config_recursion():
    """graph_config 携带 recursion_limit, 与 settings 单一来源。"""
    from lawApp_LangGraph.FastAPI.utils import graph_config
    from lawApp_LangGraph.config import settings

    cfg = graph_config("s-r1")
    assert cfg["configurable"]["thread_id"] == "s-r1"
    assert cfg["recursion_limit"] == settings.recursion_limit


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


def test_state_dead_fields_removed():
    """9 个死字段已删; 存活字段(有消费方/B预留)仍在。

    pydantic v2 字段不在类属性命名空间(hasattr 恒 False), 经 model_fields 断言。
    """
    from lawApp_LangGraph.state import AgentState

    fields = AgentState.model_fields
    dead = (
        "session_id", "user_id", "is_law_questions", "is_simple_questions",
        "should_continue", "crag_context", "memory_results", "memory_update",
        "is_pdf_output",
    )
    for f in dead:
        assert f not in fields, f"死字段未删: {f}"
    for alive in (
        "final_prompts", "pdf_path", "reasoning", "hitl_event",
        "case_elements", "clarify_history", "pending_questions",
    ):
        assert alive in fields, f"存活字段缺失: {alive}"
