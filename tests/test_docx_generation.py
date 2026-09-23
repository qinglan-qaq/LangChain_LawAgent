"""docx 文书生成 — 模板/工具/图内流/交付端点 全链测试。"""
import re
import shutil
import zipfile
from pathlib import Path

TPL_DIR = Path(__file__).resolve().parents[1] / "data" / "doc_templates" / "complaint"


def _document_xml() -> str:
    """读取 template.docx 的 document.xml 原文。"""
    with zipfile.ZipFile(TPL_DIR / "template.docx") as z:
        return z.read("word/document.xml").decode("utf-8")


def test_template_docx_exists():
    """模板与源文件齐备: source.docx(原始附件)/template.docx(标签版)/fields.yaml。"""
    assert (TPL_DIR / "source.docx").exists()
    assert (TPL_DIR / "template.docx").exists()
    assert (TPL_DIR / "fields.yaml").exists()


def test_template_contains_all_text_tags():
    """fields.yaml 里全部 text/date 字段(含 _merge_into 附带字段)的 {{ f.* }} 必须落在模板里。"""
    import yaml

    spec = yaml.safe_load((TPL_DIR / "fields.yaml").read_text(encoding="utf-8"))
    xml = _document_xml()
    for f in spec["fields"]:
        if f["type"] == "choice":
            continue
        assert "{{ f." + f["key"] + " }}" in xml, f"缺文本标签 {f['key']}"


def test_template_contains_checkbox_tags():
    """抽查代表性勾选键(YAML 手写集的子集)。"""
    xml = _document_xml()
    for tag in ("c.plaintiff_gender_male", "c.defendant_gender_female",
                "c.property_has_none", "c.property_has_has",
                "c.debt_has_has", "c.agent_has_yes", "c.agent_has_no",
                "c.e_service_no", "c.e_service_method_other",
                "c.agent_scope_general",
                "c.compensation_damage", "c.visit_subject_plaintiff"):
        assert "{{ " + tag + " }}" in xml, f"缺勾选标签 {tag}"


def test_template_contains_all_handwritten_checkbox_tags():
    """fields.yaml 手写的每个 {{ c.* }}(以 replacement 实际所写为准)都必须落在模板里。"""
    import yaml

    spec = yaml.safe_load((TPL_DIR / "fields.yaml").read_text(encoding="utf-8"))
    xml = _document_xml()
    for f in spec["fields"]:
        for ckey in re.findall(r"\{\{\s*c\.([A-Za-z0-9_]+)\s*\}\}", f.get("replacement", "")):
            assert "{{ c." + ckey + " }}" in xml, f"缺勾选标签 {ckey} (字段 {f['key']})"
        if f.get("type") == "choice":
            assert re.search(r"\{\{ c\." + re.escape(f["key"]) + r"_[A-Za-z0-9_]+ \}\}", xml), \
                f"choice 字段 {f['key']} 无任何勾选标签"
