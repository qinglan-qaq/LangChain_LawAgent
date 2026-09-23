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


# ---- Task 3: generate_docx 工具 ----

import json as _json


def _run_tool(fields: dict, doc_type: str = "complaint", filename: str = "") -> dict:
    import asyncio
    from lawApp_LangGraph.tools.tools import generate_docx
    return asyncio.run(generate_docx.ainvoke(
        {"fields_json": _json.dumps(fields, ensure_ascii=False), "doc_type": doc_type, "filename": filename}
    ))


def _doc_text(docx_path) -> str:
    """取渲染产物全部可见文本: 表格嵌套逐层展开(模板是法院表格, 顶层段落无字段)。"""
    import docx  # docxtpl 带的 python-docx
    d = docx.Document(str(docx_path))
    parts = [p.text for p in d.paragraphs]

    def walk(table) -> None:
        for row in table.rows:
            for cell in row.cells:
                parts.extend(p.text for p in cell.paragraphs)
                for nested in cell.tables:
                    walk(nested)

    for t in d.tables:
        walk(t)
    return "\n".join(parts)


def test_generate_docx_full_fill(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCX_OUTPUT_DIR", str(tmp_path))
    fields = {"plaintiff_name": "张三", "plaintiff_gender": "男",
              "defendant_name": "李四", "defendant_gender": "女",
              "fact_divorce_reason": "感情破裂分居两年", "evidence_list": "结婚证、分居证明"}
    r = _run_tool(fields, filename="起诉状_AT-1.docx")
    assert r["status"] == "success" and r["docx_path"]
    import os
    assert os.path.exists(r["docx_path"])
    assert r["filled"] >= 6


def test_generate_docx_missing_text_becomes_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCX_OUTPUT_DIR", str(tmp_path))
    r = _run_tool({"plaintiff_name": "张三"})
    assert r["status"] == "success" and r["pending"] > 0
    # 产物里文本空位 = "待补充"
    assert "待补充" in _doc_text(r["docx_path"])


def test_generate_docx_choice_checkboxes(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCX_OUTPUT_DIR", str(tmp_path))
    r = _run_tool({"plaintiff_gender": "男", "property_has": "有财产"})
    assert r["status"] == "success"
    text = _doc_text(r["docx_path"])
    assert "☑男 ☐女" in text
    assert "☐无财产" in text and "☑有财产" in text


def test_generate_docx_filename_sanitized(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCX_OUTPUT_DIR", str(tmp_path))
    r = _run_tool({"plaintiff_name": "x"}, filename="../../evil/z..docx")
    assert r["status"] == "success"
    import os
    assert "evil" not in r["docx_path"].replace("\\", "/")
    assert os.path.dirname(os.path.abspath(r["docx_path"])) == str(tmp_path.resolve())


def test_generate_docx_merge_into_fields_rendered(tmp_path, monkeypatch):
    """I-1(R1): _merge_into 附带字段值须渲染(标签在宿主 replacement 内),
    缺失给"待补充"(spec D4); 仅 filled/pending 计数跳过。"""
    monkeypatch.setenv("DOCX_OUTPUT_DIR", str(tmp_path))
    # 传附带字段值: 渲染产物须含各值
    r = _run_tool({"plaintiff_work": "某公司", "plaintiff_duty": "经理",
                   "plaintiff_phone": "13800000000", "preservation_court": "海淀法院"})
    assert r["status"] == "success"
    text = _doc_text(r["docx_path"])
    for needle in ("经理", "13800000000", "海淀法院"):
        assert needle in text, f"附带字段值未被渲染: {needle}"
    # 只传宿主: 附带字段空位 = "待补充"(D4)
    r2 = _run_tool({"plaintiff_work": "某公司"})
    assert r2["status"] == "success"
    assert "职务：待补充" in _doc_text(r2["docx_path"])


def test_generate_docx_unknown_doctype_error(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCX_OUTPUT_DIR", str(tmp_path))
    r = _run_tool({"a": "b"}, doc_type="defense")
    assert r["status"] == "error" and r["docx_path"] is None


def test_build_docx_template_reproducible(tmp_path):
    """I-2(deferred): 重建路径可复现 — 同源同 YAML 重跑 build 脚本,
    产物 document.xml 与已提交 template.docx 逐字节一致。"""
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[1]
    out = tmp_path / "template.docx"
    r = subprocess.run(
        [sys.executable, str(root / "scripts" / "build_docx_template.py"),
         "--src", str(TPL_DIR / "source.docx"),
         "--yaml", str(TPL_DIR / "fields.yaml"),
         "--out", str(out)],
        capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, r.stderr
    with zipfile.ZipFile(out) as z:
        rebuilt = z.read("word/document.xml")
    assert rebuilt == _document_xml().encode("utf-8")
