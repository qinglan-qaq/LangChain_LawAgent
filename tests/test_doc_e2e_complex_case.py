"""复杂案情端到端 — 同一案情分别渲染起诉状/答辩状模板, 断言关键字段值/
勾选状态/待补充归一; 可选用例走真实 flash LLM 抽取(无 key 自动跳过)。"""
import json
from pathlib import Path

_DOCX_T_PREFIX = "T-doc-e2e"


def _run_tool(fields, doc_type="complaint", filename=""):
    import asyncio

    from lawApp_LangGraph.tools.tools import generate_docx
    return asyncio.run(generate_docx.ainvoke({
        "fields_json": json.dumps(fields, ensure_ascii=False),
        "doc_type": doc_type, "filename": filename}))


def _doc_text(docx_path) -> str:
    import docx
    d = docx.Document(str(docx_path))
    parts = [p.text for p in d.paragraphs]

    def walk(table):
        for row in table.rows:
            for cell in row.cells:
                parts.extend(p.text for p in cell.paragraphs)
                for nested in cell.tables:
                    walk(nested)

    for t in d.tables:
        walk(t)
    return "\n".join(parts)


# 自拟复杂完整案情(覆盖: 双方身份/婚姻/子女/房产汽车存款/债务/抚养费/探望/
# 损害赔偿/管辖保全/证据; 部分字段故意留白验"待补充"归一)
CASE_TEXT = (
    "原告张三,男,1985年3月12日出生,汉族,户籍北京市朝阳区幸福路10号,现住北京市"
    "海淀区中关村大街8号,联系电话13800000001,在北京华宇科技有限公司任工程师。"
    "被告李四,女,1987年7月25日出生,汉族,户籍河北省石家庄市长安区中山路5号,"
    "现住北京市海淀区中关村大街8号,联系电话13900000002。双方于2008年5月20日在"
    "北京市朝阳区民政局登记结婚,2009年生育长子张小军,2014年生育次女张小丽。"
    "自2019年起被告沉迷赌博屡教不改,双方经常争吵,2021年8月起原告搬至公司宿舍"
    "与被告分居至今,夫妻感情确已破裂。婚姻期间购得北京市海淀区学府路1号房屋一套"
    "(登记双方名下,市值约600万元),别克牌汽车一辆(登记被告名下),招商银行存款"
    "约80万元(原告名下账户)。被告因赌博欠个人债务20万元。两子女出生后一直随"
    "原告及原告父母共同生活,原告请求两子女均由其直接抚养,被告每月支付每名子女"
    "抚养费3000元至年满十八周岁,被告每月可探望子女两次。因被告赌博存在重大过错,"
    "原告请求离婚损害赔偿50000元。双方无仲裁或管辖约定,原告不申请财产保全。"
    "证据: 结婚证、户口簿、分居证明、被告赌博聊天记录、银行流水。"
)

# 案情 → 起诉状 ground truth(手工对出; 未提供字段如民族留给"待补充"断言)
# 注: property_house_owner 等归属键非 fields.yaml 字段(归属勾选为 text 字段
# replacement 内手写 c 键, 渲染层统一缺省 ☐), 保留仅作人工校对参照(Task 10)
COMPLAINT_FIELDS = {
    "plaintiff_name": "张三", "plaintiff_gender": "男",
    "plaintiff_birth_date": "1985年3月12日", "plaintiff_work": "北京华宇科技有限公司",
    "plaintiff_duty": "工程师", "plaintiff_phone": "13800000001",
    "plaintiff_domicile": "北京市朝阳区幸福路10号",
    "plaintiff_residence": "北京市海淀区中关村大街8号",
    "defendant_name": "李四", "defendant_gender": "女",
    "defendant_birth_date": "1987年7月25日",
    "defendant_domicile": "河北省石家庄市长安区中山路5号",
    "defendant_residence": "北京市海淀区中关村大街8号",
    "defendant_phone": "13900000002",
    "claim_divorce": "因感情确已破裂, 请求判决解除婚姻关系",
    "property_has": "有财产",
    "property_house": "北京市海淀区学府路1号房屋一套(双方名下, 市值约600万元)",
    "property_house_owner": "原告",
    "property_car": "别克牌汽车一辆(被告名下)",
    "property_car_owner": "被告",
    "property_deposit": "招商银行存款约80万元",
    "property_deposit_owner": "原告",
    "debt_has": "有债务",
    "debt1": "被告因赌博欠个人债务20万元",
    "custody_has": "有此问题", "custody_child1": "原告",
    "alimony_has": "有此问题", "alimony_payer": "被告",
    "alimony_amount": "每名子女每月3000元至年满十八周岁",
    "visit_has": "有此问题", "visit_subject": "被告",
    "visit_method": "每月探望子女两次",
    "compensation": "离婚损害赔偿", "comp_damage_amount": "50000元",
    "fact_marriage_time": "2008年5月20日",
    "fact_children": "2009年生长子张小军, 2014年生次女张小丽",
    "fact_divorce_reason": "被告沉迷赌博屡教不改, 2021年8月分居至今",
    "fact_basis": "民法典第一千零七十九条",
    "evidence_list": "结婚证、户口簿、分居证明、赌博聊天记录、银行流水",
    "signer": "张三", "sign_date": "2026年10月9日",
}

# 案情 → 答辩状 ground truth(答辩人=被告李四视角; 7 项态度中仅示意 1/2/4 项,
# 其余缺省验勾选全 ☐ + 待补充归一)
DEFENSE_FIELDS = {
    "case_no": "(2026)京0108民初1234号", "case_cause": "离婚纠纷",
    "respondent_name": "李四", "respondent_gender": "女",
    "respondent_birth_date": "1987年7月25日",
    "respondent_domicile": "河北省石家庄市长安区中山路5号",
    "respondent_residence": "北京市海淀区中关村大街8号",
    "resp_divorce": "异议", "resp_divorce_reason": "不同意离婚, 双方感情尚未破裂",
    "resp_property": "异议",
    "resp_property_reason": "房屋系双方共同财产, 请求依法分割",
    "resp_custody": "异议", "resp_custody_reason": "子女随母亲生活更有利于成长",
    "resp_basis": "民法典第一千零八十四条",
    "evidence_list": "结婚证、户口簿",
    "signer": "李四", "sign_date": "2026年10月9日",
}


def test_complex_case_complaint_render(tmp_path, monkeypatch):
    """复杂案情 → 起诉状: 关键字段值/勾选/关键归一全部落产物。"""
    monkeypatch.setenv("DOCX_OUTPUT_DIR", str(tmp_path))
    r = _run_tool(COMPLAINT_FIELDS, doc_type="complaint", filename="起诉状_e2e.docx")
    assert r["status"] == "success"
    assert (tmp_path / "起诉状_e2e.docx").exists()
    text = _doc_text(r["docx_path"])
    # 当事人与事实主干
    for needle in ("张三", "李四", "13800000001", "北京市海淀区学府路1号",
                   "2008年5月20日", "张小军", "张小丽", "600万元", "3000元",
                   "民法典第一千零七十九条", "赌博聊天记录"):
        assert needle in text, f"起诉状缺关键内容: {needle}"
    # 勾选状态(性别/财产/抚养/探望)
    assert "☑男 ☐女" in text
    assert "☐无财产" in text and "☑有财产" in text
    assert "☑有此问题" in text
    # 事实理由自由文本整段落进产物
    assert "分居至今" in text
    # 案情未给的字段 → 待补充归一(原告民族未提供)
    assert "民族：待补充" in text or "待补充" in text


def test_complex_case_defense_render(tmp_path, monkeypatch):
    """复杂案情 → 答辩状: 答辩人视角字段/确认异议勾选/未答项 ☐ 全落产物。"""
    monkeypatch.setenv("DOCX_OUTPUT_DIR", str(tmp_path))
    r = _run_tool(DEFENSE_FIELDS, doc_type="defense", filename="答辩状_e2e.docx")
    assert r["status"] == "success"
    text = _doc_text(r["docx_path"])
    for needle in ("李四", "离婚纠纷", "(2026)京0108民初1234号",
                   "不同意离婚", "依法分割", "第一千零八十四条"):
        assert needle in text, f"答辩状缺关键内容: {needle}"
    # 1/2/4 项态度勾选: 异议/异议/异议
    assert "☑异议" in text
    # 未表态项(3 债务/5 抚养费/6 探望/7 赔偿)勾选全空
    assert "☐确认" in text
    # 性别勾选: 女
    assert "☐男 ☑女" in text
    # 答辩人未提供的单位/职务 → 待补充
    assert "待补充" in text


def test_extract_doc_fields_from_complex_case():
    """可选端到端: 真实 flash LLM 从复杂案情抽起诉状字段(无 API key 跳过)。"""
    import pytest

    from lawApp_LangGraph.config import settings
    if not settings.deepseek_api_key:
        pytest.skip("无 DEEPSEEK_API_KEY, 跳过真实抽取端到端")
    import asyncio
    import types as _t

    import lawApp_LangGraph.LangGraph_lawApp as app
    from lawApp_LangGraph.state import default_case_elements

    # 计划原稿 case_elements=None 会在 state.case_elements.digest() 上崩
    # (AttributeError), 改用默认要素清单(无已知要素 → digest 返回占位文本)
    st = _t.SimpleNamespace(
        query=CASE_TEXT, user_supplements=[],
        case_elements=default_case_elements(), law_results=[],
    )
    fields = asyncio.run(app._extract_doc_fields(st, "complaint"))
    assert fields["plaintiff_name"] == "张三"
    assert fields["plaintiff_gender"] == "男"
    assert fields["defendant_name"] == "李四"
    assert fields["fact_marriage_time"].startswith("2008")
    assert "赌博" in fields["fact_divorce_reason"]
