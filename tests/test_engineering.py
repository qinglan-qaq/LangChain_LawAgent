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


def test_graph_config_recursion():
    """graph_config 携带 recursion_limit, 与 settings 单一来源。"""
    from lawApp_LangGraph.FastAPI.utils import graph_config
    from lawApp_LangGraph.config import settings

    cfg = graph_config("s-r1")
    assert cfg["configurable"]["thread_id"] == "s-r1"
    assert cfg["recursion_limit"] == settings.recursion_limit
