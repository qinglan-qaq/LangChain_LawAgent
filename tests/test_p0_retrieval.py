"""P0 检索修复 — namespace 参数化(规格决策 18)。"""
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_namespace_setting_default_law_cases(monkeypatch):
    from lawApp_LangGraph.config import Settings

    monkeypatch.delenv("PINECONE_NAMESPACE", raising=False)
    assert Settings(_env_file=None).pinecone_namespace == "law_cases"


def test_namespace_setting_env_override(monkeypatch):
    from lawApp_LangGraph.config import Settings

    monkeypatch.setenv("PINECONE_NAMESPACE", "Law_test_namespace")
    assert Settings(_env_file=None).pinecone_namespace == "Law_test_namespace"


def test_retriever_signature_namespace_default_none():
    """硬编码 'law_cases' 全线清除: 签名默认 None, 运行时 settings 兜底。"""
    from lawApp_LangGraph.RAG_service.base import BaseRetriever
    from lawApp_LangGraph.RAG_service.pinecone_retriever import PineconeRetriever

    for fn in (BaseRetriever.search, PineconeRetriever.search):
        assert inspect.signature(fn).parameters["namespace"].default is None
