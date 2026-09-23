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


# ---- Task 2: doc_templates 加载/对账 ----


def test_load_fields_complaint():
    from lawApp_LangGraph.doc_templates import load_fields
    fields = load_fields("complaint")
    assert fields, "complaint 模板应可加载"
    keys = {f["key"] for f in fields}
    assert {"plaintiff_name", "defendant_name", "fact_divorce_reason"} <= keys


def test_load_fields_missing_returns_empty():
    from lawApp_LangGraph.doc_templates import load_fields
    assert load_fields("defense") == []  # 本期无答辩状模板 → 空表(不抛)


def test_validate_template_complaint_ok():
    from lawApp_LangGraph.doc_templates import validate_template
    ok, missing = validate_template("complaint")
    assert ok, f"对账不应有缺口: {missing}"


def test_validate_template_detects_missing_tag(tmp_path):
    import lawApp_LangGraph.doc_templates as dt
    # 破坏副本: 复制 complaint 目录到 tmp, 删掉一个文本标签, 指向副本校验
    src = dt._TEMPLATE_ROOT / "complaint"
    dst = tmp_path / "broken"
    shutil.copytree(src, dst)
    doc = dst / "template.docx"
    # 先整体读入(避免同文件边读边写被 "w" 截断), 再重写破坏版
    with zipfile.ZipFile(doc) as zin:
        xml = zin.read("word/document.xml").decode("utf-8")
        payload = [(i.filename, zin.read(i.filename)) for i in zin.infolist()]
    xml = xml.replace("{{ f.plaintiff_name }}", "", 1)
    with zipfile.ZipFile(doc, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, data in payload:
            if name == "word/document.xml":
                data = xml.encode("utf-8")
            zout.writestr(name, data)
    ok, missing = dt._validate_dir(dst)
    assert not ok and "f.plaintiff_name" in missing
