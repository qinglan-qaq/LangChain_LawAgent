"""field_clarify 字段补全循环 — 状态/配置/循环/落库 全链测试。"""


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
