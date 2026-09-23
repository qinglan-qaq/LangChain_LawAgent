# 律师助理 Word 文书生成 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** assistant 模式下按法院表格式模板(《离婚民事起诉状》)生成填充好的 docx 文书,HITL 确认(带字段预览)后交付下载。

**Architecture:** 镜像 markdown_to_pdf 的 HITL-3 链——planner 末步规划 `generate_docx` 工具步骤 → executor 先 flash LLM 结构化抽取模板字段,再 raise `docx_confirm` interrupt(选项:确认生成/跳过,载荷带字段预览)→ 确认后直接构造 tool_calls(不走 LLM 提参)→ ToolNode 执行 docxtpl 渲染 → merge 回填 `docx_path` + 落库 → SSE `docx_done` 帧 → 前端弹窗/toast/下载按钮。模板体系 = `data/doc_templates/complaint/` 下 template.docx(docxtpl 标签版)+ fields.yaml(结构定义),加新文书零代码。

**Tech Stack:** docxtpl(带 python-docx; jinja2 已有)、LangGraph interrupt、SSE、Vue3 + Tailwind。

**Spec:** `docs/superpowers/specs/2026-09-23-assistant-docx-generation-design.md`(8 项决策 D1-D8 + 8 节设计,实施前先读)

## Global Constraints

- Python 一律用 `F:\Anaconda_env\lawApp_LangGraph\python.exe`(下文 `$PY` 代指);pytest 跑法 `$PY -m pytest tests/<file>.py -v`
- 真实 PG:localhost:15432 / Law_app / 凭据在 `lawApp_LangGraph/.env`(起 assumed running;若连不上,任务里涉及 PG 的用例按仓库惯例 `scripts/run_pytest_timeout_skip.py` 超时跳过并注明)
- 前端构建:`cd frontend && npm run build`(必须 0 error)
- 提交风格沿仓库惯例:`C: <主题> — <细节>` 中文一行式,小步提交
- 所有新代码中文 docstring,风格对齐被改文件
- SSE 事件名/interrupt 类型字符串**不可改动已有值**;新增值:`docx_confirm`、`docx_done`、`docx_generated`
- 勾选符号统一:`☑`(选中)/`☐`(未选);缺失文本字段统一填 `待补充`;缺失 choice 字段全部 `☐`
- 输出目录:`./docx_outputs/`(镜像 `./pdf_outputs/`)

---

### Task 1: 模板资产 + 转换脚本

**Files:**
- Create: `data/doc_templates/complaint/source.docx`(从 `E:\桌面\婚姻案件MD\离婚民事起诉状.docx` 复制)
- Create: `data/doc_templates/complaint/fields.yaml`
- Create: `scripts/build_docx_template.py`
- Create: `data/doc_templates/complaint/template.docx`(脚本产出)
- Test: `tests/test_docx_generation.py`

**Interfaces:**
- Consumes: 附件原文 docx(129 段、2 表格,文本已在 spec §Problem 描述)
- Produces: `fields.yaml` 字段条目格式 `{key, label, type: text|choice|date, critical, options, anchor, anchor_mode: text|xml, occurrence, replacement}`——Task 2/3/4 依赖此结构;`template.docx` 内含 `{{ f.KEY }}` 文本标签与 `{{ c.KEY_OPT }}` 勾选标签

- [ ] **Step 1: 复制源文件**

```bash
mkdir -p data/doc_templates/complaint
cp "E:\桌面\婚姻案件MD\离婚民事起诉状.docx" data/doc_templates/complaint/source.docx
```

- [ ] **Step 2: 写 fields.yaml(完整字段表,anchor 为正则,occurrence 为 0 基文档序)**

```yaml
# 离婚民事起诉状 — 字段结构定义
# anchor: 在 document.xml 中定位的正则(text 模式按可见文本, xml 模式按原始 XML);
# replacement 缺省时 text 类型自动为 anchor + "{{ f.KEY }}";
# occurrence: 同一 anchor 第几次出现(0 基, 按文档顺序), 缺省 0
doc_type: complaint
template: template.docx
label: 民事起诉状（离婚纠纷）
fields:
  # ---- 当事人信息: 原告 ----
  - {key: plaintiff_name, label: 原告姓名, type: text, critical: true,
     anchor: '姓名：', occurrence: 0}
  - {key: plaintiff_gender, label: 原告性别, type: choice, options: [男, 女], critical: true,
     anchor: '性别：男 女', occurrence: 0,
     replacement: '性别：{{ c.plaintiff_gender_male }}男 {{ c.plaintiff_gender_female }}女'}
  - {key: plaintiff_birth_date, label: 原告出生日期, type: date,
     anchor: '出生日期：\s*年\s*月\s*日', occurrence: 0,
     replacement: '出生日期：{{ f.plaintiff_birth_date }}'}
  - {key: plaintiff_ethnic, label: 原告民族, type: text,
     anchor: '民族：', occurrence: 0}
  - {key: plaintiff_work, label: 原告工作单位, type: text,
     anchor: '工作单位：\s*职务：\s*联系电话：', occurrence: 0,
     replacement: '工作单位：{{ f.plaintiff_work }}  职务：{{ f.plaintiff_duty }}  联系电话：{{ f.plaintiff_phone }}'}
  - {key: plaintiff_duty, label: 原告职务, type: text, _merge_into: plaintiff_work}
  - {key: plaintiff_phone, label: 原告联系电话, type: text, _merge_into: plaintiff_work}
  - {key: plaintiff_domicile, label: 原告户籍地, type: text,
     anchor: '住所地（户籍所在地）：', occurrence: 0}
  - {key: plaintiff_residence, label: 原告经常居住地, type: text,
     anchor: '经常居住地：', occurrence: 0}
  # ---- 委托诉讼代理人 ----
  - {key: agent_has, label: 有无委托代理人, type: choice, options: [有, 无],
     anchor: '<w:t>有</w:t>', anchor_mode: xml, occurrence: 0,
     replacement: '<w:t>{{ c.agent_has_yes }}有</w:t>'}
  - {key: agent_name, label: 代理人姓名, type: text,
     anchor: '姓名：', occurrence: 1}
  - {key: agent_org, label: 代理人单位, type: text,
     anchor: '单位：\s*职务：\s*联系电话：', occurrence: 0,
     replacement: '单位：{{ f.agent_org }}  职务：{{ f.agent_duty }}  联系电话：{{ f.agent_phone }}'}
  - {key: agent_duty, label: 代理人职务, type: text, _merge_into: agent_org}
  - {key: agent_phone, label: 代理人电话, type: text, _merge_into: agent_org}
  - {key: agent_scope, label: 代理权限, type: choice, options: [一般授权, 特别授权],
     anchor: '代理权限：一般授权  特别授权', occurrence: 0,
     replacement: '代理权限：{{ c.agent_scope_general }}一般授权  {{ c.agent_scope_special }}特别授权'}
  # ---- 送达地址 ----
  - {key: service_addr, label: 送达地址, type: text,
     anchor: '地址：', occurrence: 0}
  - {key: service_receiver, label: 送达收件人, type: text,
     anchor: '收件人：', occurrence: 0}
  - {key: service_receiver_phone, label: 送达收件电话, type: text,
     anchor: '电话：', occurrence: 0}
  - {key: e_service, label: 是否接受电子送达, type: choice, options: [是, 否],
     anchor: '<w:t>是  方式：', anchor_mode: xml, occurrence: 0,
     replacement: '<w:t>{{ c.e_service_yes }}是  方式：'}
  - {key: e_service_method, label: 电子送达方式, type: choice,
     options: [短信, 微信, 传真, 邮箱, 其他],
     anchor: '方式：\s*短信\s*微信\s*传真\s*邮箱', occurrence: 0,
     replacement: '方式：{{ c.e_service_method_sms }}短信  {{ c.e_service_method_wechat }}微信  {{ c.e_service_method_fax }}传真  {{ c.e_service_method_email }}邮箱'}
  # ---- 当事人信息: 被告 ----
  - {key: defendant_name, label: 被告姓名, type: text, critical: true,
     anchor: '姓名：', occurrence: 2}
  - {key: defendant_gender, label: 被告性别, type: choice, options: [男, 女], critical: true,
     anchor: '性别：男 女', occurrence: 1,
     replacement: '性别：{{ c.defendant_gender_male }}男 {{ c.defendant_gender_female }}女'}
  - {key: defendant_birth_date, label: 被告出生日期, type: date,
     anchor: '出生日期：\s*年\s*月\s*日', occurrence: 1,
     replacement: '出生日期：{{ f.defendant_birth_date }}'}
  - {key: defendant_ethnic, label: 被告民族, type: text,
     anchor: '民族：', occurrence: 1}
  - {key: defendant_work, label: 被告工作单位, type: text,
     anchor: '工作单位：\s*职务：\s*联系电话：', occurrence: 1,
     replacement: '工作单位：{{ f.defendant_work }}  职务：{{ f.defendant_duty }}  联系电话：{{ f.defendant_phone }}'}
  - {key: defendant_duty, label: 被告职务, type: text, _merge_into: defendant_work}
  - {key: defendant_phone, label: 被告联系电话, type: text, _merge_into: defendant_work}
  - {key: defendant_domicile, label: 被告户籍地, type: text,
     anchor: '住所地（户籍所在地）：', occurrence: 1}
  - {key: defendant_residence, label: 被告经常居住地, type: text,
     anchor: '经常居住地：', occurrence: 1}
  # ---- 诉讼请求 ----
  - {key: claim_divorce, label: 解除婚姻关系具体主张, type: text, critical: true,
     anchor: '1.解除婚姻关系', occurrence: 0,
     replacement: '1.解除婚姻关系\n{{ f.claim_divorce }}'}
  - {key: property_has, label: 有无共同财产, type: choice, options: [无财产, 有财产],
     anchor: '无财产', occurrence: 0,
     replacement: '{{ c.property_has_none }}无财产'}
  - {key: property_house, label: 房屋明细, type: text,
     anchor: '（1）房屋明细：归属：原告 /被告/其他', occurrence: 0,
     replacement: '（1）房屋明细：{{ f.property_house }} 归属：{{ c.property_house_owner_plaintiff }}原告 {{ c.property_house_owner_defendant }}/被告{{ c.property_house_owner_other }}/其他'}
  - {key: property_car, label: 汽车明细, type: text,
     anchor: '（2）汽车明细：归属：原告 /被告/其他', occurrence: 0,
     replacement: '（2）汽车明细：{{ f.property_car }} 归属：{{ c.property_car_owner_plaintiff }}原告 {{ c.property_car_owner_defendant }}/被告{{ c.property_car_owner_other }}/其他'}
  - {key: property_deposit, label: 存款明细, type: text,
     anchor: '（3）存款明细：归属：原告 /被告/其他', occurrence: 0,
     replacement: '（3）存款明细：{{ f.property_deposit }} 归属：{{ c.property_deposit_owner_plaintiff }}原告 {{ c.property_deposit_owner_defendant }}/被告{{ c.property_deposit_owner_other }}/其他'}
  - {key: property_other, label: 其他财产, type: text,
     anchor: '（4）其他（按照上述样式列明）', occurrence: 0,
     replacement: '（4）其他：{{ f.property_other }}'}
  - {key: debt_has, label: 有无共同债务, type: choice, options: [无债务, 有债务],
     anchor: '无债务', occurrence: 0,
     replacement: '{{ c.debt_has_none }}无债务'}
  - {key: debt1, label: 债务1, type: text,
     anchor: '（1）债务1：\s*承担主体：原告 /被告/其他', occurrence: 0,
     replacement: '（1）债务1：{{ f.debt1 }} 承担主体：{{ c.debt1_owner_plaintiff }}原告 {{ c.debt1_owner_defendant }}/被告{{ c.debt1_owner_other }}/其他'}
  - {key: custody_has, label: 有无子女抚养问题, type: choice, options: [无此问题, 有此问题],
     anchor: '4.子女直接抚养', occurrence: 0,
     replacement: '4.子女直接抚养\n{{ c.custody_has_none }}无此问题\n{{ c.custody_has_has }}有此问题'}
  - {key: custody_child1, label: 子女1归属, type: choice, options: [原告, 被告],
     anchor: '子女1：   归属：原告 /被告', occurrence: 0,
     replacement: '子女1：{{ f.custody_child1_note }} 归属：{{ c.custody_child1_plaintiff }}原告 {{ c.custody_child1_defendant }}/被告'}
  - {key: custody_child1_note, label: 子女1说明, type: text, _merge_into: custody_child1}
  - {key: alimony_has, label: 有无抚养费问题, type: choice, options: [无此问题, 有此问题],
     anchor: '5.子女抚养费', occurrence: 0,
     replacement: '5.子女抚养费\n{{ c.alimony_has_none }}无此问题\n{{ c.alimony_has_has }}有此问题'}
  - {key: alimony_payer, label: 抚养费承担主体, type: choice, options: [原告, 被告],
     anchor: '抚养费承担主体：原告 /被告', occurrence: 0,
     replacement: '抚养费承担主体：{{ c.alimony_payer_plaintiff }}原告 {{ c.alimony_payer_defendant }}/被告'}
  - {key: alimony_amount, label: 抚养费金额及明细, type: text,
     anchor: '金额及明细：', occurrence: 0}
  - {key: alimony_pay_method, label: 抚养费支付方式, type: text,
     anchor: '支付方式：', occurrence: 0}
  - {key: visit_has, label: 有无探望权问题, type: choice, options: [无此问题, 有此问题],
     anchor: '6.探望权', occurrence: 0,
     replacement: '6.探望权\n{{ c.visit_has_none }}无此问题\n{{ c.visit_has_has }}有此问题'}
  - {key: visit_subject, label: 探望权行使主体, type: choice, options: [原告, 被告],
     anchor: '探望权行使主体：原告 /被告', occurrence: 0,
     replacement: '探望权行使主体：{{ c.visit_subject_plaintiff }}原告 {{ c.visit_subject_defendant }}/被告'}
  - {key: visit_method, label: 探望权行使方式, type: text,
     anchor: '行使方式：', occurrence: 0}
  - {key: compensation, label: 赔偿/补偿/经济帮助, type: choice,
     options: [无此问题, 离婚损害赔偿, 离婚经济补偿, 离婚经济帮助],
     anchor: '7.离婚损害赔偿／离婚经济补偿／离婚经济帮助', occurrence: 0,
     replacement: '7.离婚损害赔偿／离婚经济补偿／离婚经济帮助\n{{ c.compensation_none }}无此问题\n{{ c.compensation_damage }}离婚损害赔偿 金额：{{ f.comp_damage_amount }}\n{{ c.compensation_econ }}离婚经济补偿 金额：{{ f.comp_econ_amount }}\n{{ c.compensation_help }}离婚经济帮助 金额：{{ f.comp_help_amount }}'}
  - {key: comp_damage_amount, label: 损害赔偿金额, type: text, _merge_into: compensation}
  - {key: comp_econ_amount, label: 经济补偿金额, type: text, _merge_into: compensation}
  - {key: comp_help_amount, label: 经济帮助金额, type: text, _merge_into: compensation}
  - {key: court_fee, label: 诉讼费用, type: text,
     anchor: '8.诉讼费用', occurrence: 0,
     replacement: '8.诉讼费用\n{{ f.court_fee }}'}
  - {key: claim_other, label: 其他请求, type: text,
     anchor: '9.本表未列明的其他请求', occurrence: 0,
     replacement: '9.本表未列明的其他请求\n{{ f.claim_other }}'}
  # ---- 约定管辖和诉讼保全 ----
  - {key: jurisdiction_has, label: 有无管辖约定, type: choice, options: [有, 无],
     anchor: '1.有无仲裁、法院管辖约定', occurrence: 0,
     replacement: '1.有无仲裁、法院管辖约定\n{{ c.jurisdiction_has_yes }}有  合同条款及内容：{{ f.jurisdiction_clause }}\n{{ c.jurisdiction_has_no }}无'}
  - {key: jurisdiction_clause, label: 管辖条款, type: text, _merge_into: jurisdiction_has}
  - {key: preservation, label: 财产保全, type: choice, options: [诉前保全, 诉讼保全, 无],
     anchor: '2.是否申请财产保全措施', occurrence: 0,
     replacement: '2.是否申请财产保全措施\n{{ c.preservation_pre }}已经诉前保全：保全法院：{{ f.preservation_court }} 保全时间：{{ f.preservation_time }}\n{{ c.preservation_sue }}申请诉讼保全\n{{ c.preservation_none }}否'}
  - {key: preservation_court, label: 保全法院, type: text, _merge_into: preservation}
  - {key: preservation_time, label: 保全时间, type: text, _merge_into: preservation}
  # ---- 事实和理由 ----
  - {key: fact_marriage_time, label: 结婚时间, type: text,
     anchor: '结婚时间：', occurrence: 0}
  - {key: fact_children, label: 生育子女情况, type: text,
     anchor: '生育子女情况：', occurrence: 0}
  - {key: fact_life, label: 双方生活情况, type: text,
     anchor: '双方生活情况：', occurrence: 0}
  - {key: fact_divorce_reason, label: 离婚事由, type: text, critical: true,
     anchor: '离婚事由：', occurrence: 0}
  - {key: fact_prior_suit, label: 之前有无离婚诉讼, type: text,
     anchor: '之前有无提起过离婚诉讼：', occurrence: 0}
  - {key: fact_property, label: 财产事实理由, type: text,
     anchor: '2.夫妻共同财产情况', occurrence: 0,
     replacement: '2.夫妻共同财产情况\n{{ f.fact_property }}'}
  - {key: fact_debt, label: 债务事实理由, type: text,
     anchor: '3.夫妻共同债务情况', occurrence: 0,
     replacement: '3.夫妻共同债务情况\n{{ f.fact_debt }}'}
  - {key: fact_custody, label: 抚养事实理由, type: text,
     anchor: '4.子女直接抚养情况', occurrence: 0,
     replacement: '4.子女直接抚养情况\n{{ f.fact_custody }}'}
  - {key: fact_alimony, label: 抚养费事实理由, type: text,
     anchor: '5.子女抚养费情况', occurrence: 0,
     replacement: '5.子女抚养费情况\n{{ f.fact_alimony }}'}
  - {key: fact_visit, label: 探望权事实理由, type: text,
     anchor: '6.子女探望权情况', occurrence: 0,
     replacement: '6.子女探望权情况\n{{ f.fact_visit }}'}
  - {key: fact_comp, label: 赔偿相关情况, type: text,
     anchor: '7.赔偿/补偿/经济帮助相关情况', occurrence: 0,
     replacement: '7.赔偿/补偿/经济帮助相关情况\n{{ f.fact_comp }}'}
  - {key: fact_other, label: 其他事实, type: text,
     anchor: '8.其他', occurrence: 0,
     replacement: '8.其他\n{{ f.fact_other }}'}
  - {key: fact_basis, label: 诉请依据(法条), type: text, critical: true,
     anchor: '9.诉请依据', occurrence: 0,
     replacement: '9.诉请依据\n{{ f.fact_basis }}'}
  - {key: evidence_list, label: 证据清单, type: text,
     anchor: '10.证据清单（可另附页）', occurrence: 0,
     replacement: '10.证据清单（可另附页）\n{{ f.evidence_list }}'}
  # ---- 落款 ----
  - {key: signer, label: 具状人, type: text,
     anchor: '具状人（签字、盖章）：', occurrence: 0}
  - {key: sign_date, label: 落款日期, type: date,
     anchor: '日期：', occurrence: 0}
```

注意:YAML 里 `\n` 在替换串中是字面量——转换脚本处理时把它还原为**段内换行不适用,直接当普通字符写入 w:t 不合法**;docx 的 w:t 不接受换行。上面带 `\n` 的 replacement 一律改为:脚本在写入时若含 `\n`,拆分为「在当前 `</w:p>` 后复制一个同结构空段落并分段写入」——**实现规则:带 `\n` 的 replacement,脚本把 `{{ … }}` 与锚点文本按段拆开,复用锚点所在段落的 pPr/rPr 克隆新段落**。若实现中发现锚点本身独占段落(如"1.解除婚姻关系"),则直接在锚点段落后追加克隆段落放 `{{ f.xxx }}`,不写 `\n`。允许实现者按真实 XML 调整 replacement 的拆分方式,但**每个字段最终必须把标签写进 template.docx**(由 Step 4 对账断言保证,漏一个即构建失败)。

- [ ] **Step 3: 写转换脚本 scripts/build_docx_template.py**

```python
"""把 source.docx 转成 docxtpl 模板: 按 fields.yaml 的 anchor 正则定位,
逐字段替换为 {{ f.KEY }} / {{ c.KEY_OPT }} 标签。

用法: python scripts/build_docx_template.py \
        --src data/doc_templates/complaint/source.docx \
        --yaml data/doc_templates/complaint/fields.yaml \
        --out data/doc_templates/complaint/template.docx

约束: 每个 anchor×occurrence 必须命中, 未命中即非零退出(构建期暴露, 不留静默缺口)。
"""
import argparse
import copy
import re
import shutil
import zipfile
from pathlib import Path

import yaml


def load_fields(yaml_path: Path) -> dict:
    return yaml.safe_load(yaml_path.read_text(encoding="utf-8"))


def replace_nth(xml: str, pattern: str, replacement: str, occurrence: int) -> str:
    """替换 pattern 的第 occurrence 次匹配(0 基); 找不够即 ValueError。"""
    hits = list(re.finditer(pattern, xml))
    if len(hits) <= occurrence:
        raise ValueError(f"anchor 命中不足: {pattern!r} 需要第 {occurrence} 处, 实际 {len(hits)} 处")
    m = hits[occurrence]
    return xml[: m.start()] + replacement + xml[m.end() :]


def split_to_paragraphs(xml: str, para_pPr: str, texts: list[str]) -> str:
    """多段 replacement: 克隆锚点段落结构, 每段一个 w:t。"""
    return "".join(
        f'<w:p><w:pPr>{para_pPr}</w:pPr><w:r><w:t>{t}</w:t></w:r></w:p>' for t in texts
    )
    # 实现注: para_pPr 从锚点所在段落拷贝(保持字体宋体/字号), 见 main 里的提取逻辑


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--yaml", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    spec = load_fields(Path(args.yaml))
    src = Path(args.src)
    zin = zipfile.ZipFile(src)
    xml = zin.read("word/document.xml").decode("utf-8")

    for f in spec["fields"]:
        anchor = f.get("anchor")
        if not anchor:
            continue  # _merge_into 附带字段无独立锚点
        occ = f.get("occurrence", 0)
        mode = f.get("anchor_mode", "text")
        if f.get("type") == "choice" or "replacement" in f:
            repl = f["replacement"]
        else:
            repl = anchor + "{{ f." + f["key"] + " }}"
        if mode == "xml":
            xml = replace_nth(xml, anchor, repl, occ)
        elif "\n" in repl:
            # 多段: 锚点段保留锚点原文前半, 其余拆段克隆(见文件头约束)
            parts = repl.split("\n")
            m = list(re.finditer(anchor, xml))[occ]
            para = re.search(r"<w:p[ >].*?</w:p>", xml[m.start() - 4000 : m.start()], re.S)
            # 取锚点之前最近的段落结构作为克隆模板(含 pPr 字体), 简化: 全局默认宋体 18 半磅
            ppr = '<w:widowControl/><w:jc w:val="left"/>' \
                  '<w:rPr><w:rFonts w:hint="eastAsia" w:ascii="宋体" w:hAnsi="宋体"/>' \
                  '<w:sz w:val="18"/><w:szCs w:val="18"/></w:rPr>'
            head, tail = parts[0], parts[1:]
            tail_xml = "".join(
                f'<w:p><w:pPr>{ppr}</w:pPr><w:r><w:rPr>'
                f'<w:rFonts w:hint="eastAsia" w:ascii="宋体" w:hAnsi="宋体"/>'
                f'<w:sz w:val="18"/><w:szCs w:val="18"/></w:rPr>'
                f'<w:t>{t}</w:t></w:r></w:p>' for t in tail
            )
            old = re.findall(anchor, xml)[occ]
            xml = xml.replace(old, head + '</w:t></w:r></w:p>' + tail_xml, 1) \
                if False else replace_nth(xml, anchor, head, occ)
            # 上行 replace_nth 已把锚点换成首段文本; 尾段插在锚点段 </w:p> 之后:
            m2 = re.search(re.escape(head), xml)
            close = xml.find("</w:p>", m2.end())
            xml = xml[: close + 6] + tail_xml + xml[close + 6 :]
        else:
            xml = replace_nth(xml, anchor, repl, occ)

    # 对账: 每个 key 的标签必须出现在产物里
    for f in spec["fields"]:
        if f.get("type") == "choice":
            for opt in f["options"]:
                tag = "{{ c." + f["key"] + "_" + _opt_key(opt) + " }}"
                assert tag in xml, f"缺勾选标签 {tag}"
        elif not f.get("_merge_into"):
            tag = "{{ f." + f["key"] + " }}"
            assert tag in xml, f"缺文本标签 {tag}"

    # 写出: 复制 zip, 仅替换 document.xml
    out = Path(args.out)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "word/document.xml":
                data = xml.encode("utf-8")
            zout.writestr(item, data)


def _opt_key(opt: str) -> str:
    """中文选项 → 拼音式英文键: 按固定映射表, 未收录即报错(加新文书时补表)。"""
    OPT_KEYS = {
        "男": "male", "女": "female", "有": "yes", "无": "no",
        "一般授权": "general", "特别授权": "special", "是": "yes",
        "短信": "sms", "微信": "wechat", "传真": "fax", "邮箱": "email", "其他": "other",
        "无财产": "none", "有财产": "has", "无债务": "none", "有债务": "has",
        "无此问题": "none", "有此问题": "has", "原告": "plaintiff", "被告": "defendant",
        "离婚损害赔偿": "damage", "离婚经济补偿": "econ", "离婚经济帮助": "help",
        "诉前保全": "pre", "诉讼保全": "sue",
    }
    if opt in OPT_KEYS:
        return OPT_KEYS[opt]
    # 组合 owner 键形如 "原告 /被告/其他" 的拆分场景不在此列; 单选项未收录 → 报错
    raise KeyError(f"选项未收录英文键: {opt!r}")


if __name__ == "__main__":
    main()
```

实现说明(执行者按此修整,上面代码是骨架):
- owner 类三选一(原告/被告/其他)的 c 键在 YAML replacement 里已手写(`property_house_owner_plaintiff` 等),这类**不经过** `_opt_key` 全量展开对账——对账断言改为「YAML 里手写的每个 `{{ c.xxx }}` 都存在 + 每个 choice 字段的 options 至少一个 c 键存在」。choice 字段的 options 与手写 c 键的对应关系:`有财产`→`property_has_has`、`无财产`→`property_has_none` 等,以 YAML replacement 实际所写为准。
- `custody_child1_note`、`_merge_into` 字段表示「多字段同锚点一次替换」,对账按标签字符串存在性断言即可。
- 全角空格/半角空格混合:anchor 正则统一用 `\s*`,Python3 `\s` 含全角空格 \u3000,已覆盖。

- [ ] **Step 4: 跑脚本生成 template.docx, 迭代 anchor 直到对账全过**

```bash
$PY scripts/build_docx_template.py --src data/doc_templates/complaint/source.docx --yaml data/doc_templates/complaint/fields.yaml --out data/doc_templates/complaint/template.docx
```

Expected: 退出码 0。每个 anchor 未命中会 ValueError 报具体 pattern——按报错核对 source.docx 真实文本(用 `$PY -c "import zipfile,re; xml=zipfile.ZipFile(r'data/doc_templates/complaint/source.docx').read('word/document.xml').decode(); print([t for t in re.findall(r'<w:t[^>]*>(.*?)</w:t>', xml) if 'XX' in t])"` 查锚点原文),修 YAML anchor 后重跑。

- [ ] **Step 5: 安装 docxtpl + requirements.txt**

```bash
$PY -m pip install docxtpl
```

`requirements.txt` 末尾追加一行 `docxtpl`(按文件现有格式)。

- [ ] **Step 6: 写失败测试(tests/test_docx_generation.py 首批: 模板冒烟)**

```python
"""docx 文书生成 — 模板/工具/图内流/交付端点 全链测试。"""
import re
import zipfile
from pathlib import Path

import pytest

TPL_DIR = Path(__file__).resolve().parents[1] / "data" / "doc_templates" / "complaint"


def _document_xml() -> str:
    z = zipfile.ZipFile(TPL_DIR / "template.docx")
    return z.read("word/document.xml").decode("utf-8")


def test_template_docx_exists():
    assert (TPL_DIR / "template.docx").exists()


def test_template_contains_all_text_tags():
    import yaml
    spec = yaml.safe_load((TPL_DIR / "fields.yaml").read_text(encoding="utf-8"))
    xml = _document_xml()
    for f in spec["fields"]:
        if f["type"] == "choice" or f.get("_merge_into"):
            continue
        assert "{{ f." + f["key"] + " }}" in xml, f"缺文本标签 {f['key']}"


def test_template_contains_checkbox_tags():
    xml = _document_xml()
    # 抽查代表性勾选键(YAML 手写集的子集)
    for tag in ("c.plaintiff_gender_male", "c.defendant_gender_female",
                "c.property_has_none", "c.agent_scope_general",
                "c.compensation_damage", "c.visit_subject_plaintiff"):
        assert "{{ " + tag + " }}" in xml, f"缺勾选标签 {tag}"
```

- [ ] **Step 7: 跑测试确认全过**

```bash
$PY -m pytest tests/test_docx_generation.py -v
```

Expected: 3 PASS

- [ ] **Step 8: Commit**

```bash
git add data/doc_templates scripts/build_docx_template.py tests/test_docx_generation.py requirements.txt
git commit -m "C: docx 模板资产 — 附件离婚起诉状入库(source.docx)+ fields.yaml 结构定义(60+ 字段/anchor 正则/勾选 c 键)+ build_docx_template.py 转换脚本(逐字段替换 docxtpl 标签, anchor 未命中构建期报错)+ template.docx 产物 + docxtpl 依赖 + 模板冒烟测试 3 用例"
```

---

### Task 2: 模板加载与对账模块 doc_templates.py

**Files:**
- Create: `lawApp_LangGraph/doc_templates.py`
- Test: `tests/test_docx_generation.py`(追加)

**Interfaces:**
- Consumes: Task 1 的 `fields.yaml` 结构与 `template.docx`
- Produces(Task 3/4 依赖,签名精确):
  - `load_fields(doc_type: str) -> list[dict]`(读 `data/doc_templates/{doc_type}/fields.yaml` 的 fields 列表,文件缺失返回 `[]`)
  - `template_available(doc_type: str) -> bool`(fields.yaml 与 template.docx 均存在且对账通过;结果进程内缓存)
  - `validate_template(doc_type: str) -> tuple[bool, list[str]]`→ `(ok, missing_tags)`;missing 为模板中缺失的标签清单
  - `template_path(doc_type: str) -> str`(template.docx 绝对路径字符串)

- [ ] **Step 1: 追加失败测试**

```python
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
    zin = zipfile.ZipFile(doc)
    xml = zin.read("word/document.xml").decode("utf-8")
    xml = xml.replace("{{ f.plaintiff_name }}", "", 1)
    with zipfile.ZipFile(doc, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "word/document.xml":
                data = xml.encode("utf-8")
            zout.writestr(item, data)
    ok, missing = dt._validate_dir(dst)
    assert not ok and "f.plaintiff_name" in missing
```

- [ ] **Step 2: 跑测试确认失败**

```bash
$PY -m pytest tests/test_docx_generation.py -v
```

Expected: 新 4 用例 FAIL(ModuleNotFoundError: lawApp_LangGraph.doc_templates)

- [ ] **Step 3: 实现 doc_templates.py**

```python
"""docx 模板加载与对账 — data/doc_templates/{doc_type}/ 下 fields.yaml
与 template.docx 配对; 启动/首用时对账, 缺标签即该 doc_type 不启用
(退化纯文本终答, spec §7)。加新文书 = 加目录两文件, 零代码改动。
"""
import re
import zipfile
from functools import lru_cache
from pathlib import Path
from typing import List, Tuple

import yaml

_TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "data" / "doc_templates"


def load_fields(doc_type: str) -> List[dict]:
    """读字段定义; 文件缺失返回空列表(调用方按不可用降级)。"""
    p = _TEMPLATE_ROOT / doc_type / "fields.yaml"
    if not p.exists():
        return []
    try:
        spec = yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    return list(spec.get("fields") or [])


def template_path(doc_type: str) -> str:
    return str(_TEMPLATE_ROOT / doc_type / "template.docx")


def _validate_dir(dir_path: Path) -> Tuple[bool, List[str]]:
    """对账: YAML 每字段标签须出现在 template.docx(document.xml 文本)。"""
    yaml_p = dir_path / "fields.yaml"
    doc_p = dir_path / "template.docx"
    if not (yaml_p.exists() and doc_p.exists()):
        return False, ["fields.yaml 或 template.docx 缺失"]
    spec = yaml.safe_load(yaml_p.read_text(encoding="utf-8"))
    xml = zipfile.ZipFile(doc_p).read("word/document.xml").decode("utf-8")
    missing = []
    for f in spec.get("fields") or []:
        if f.get("type") == "choice" or f.get("_merge_into"):
            # 勾选键以 YAML 手写 replacement 为准: 收集该字段 replacement 里全部 {{ c.* }}
            repl = f.get("replacement") or ""
            for ckey in re.findall(r"\{\{\s*(c\.[\w]+)\s*\}\}", repl):
                if "{{ " + ckey + " }}" not in xml:
                    missing.append(ckey)
        else:
            tag = "{{ f." + f["key"] + " }}"
            if tag not in xml:
                missing.append("f." + f["key"])
    return (not missing), missing


@lru_cache(maxsize=8)
def validate_template(doc_type: str) -> Tuple[bool, Tuple[str, ...]]:
    ok, missing = _validate_dir(_TEMPLATE_ROOT / doc_type)
    return ok, tuple(missing)


def template_available(doc_type: str) -> bool:
    """模板可用性(进程内缓存); 对账失败记 stderr warning 并返回 False。"""
    ok, missing = validate_template(doc_type)
    if not ok:
        import sys
        print(f"[doc_templates] {doc_type} 模板不可用: {missing}", file=sys.stderr)
    return ok
```

- [ ] **Step 4: 跑测试确认全过(测试文件头部补 `import shutil`)**

```bash
$PY -m pytest tests/test_docx_generation.py -v
```

Expected: 7 PASS

- [ ] **Step 5: Commit**

```bash
git add lawApp_LangGraph/doc_templates.py tests/test_docx_generation.py
git commit -m "C: doc_templates.py — load_fields/template_path/validate_template(YAML↔docx 标签双向对账, choice 走 replacement 手写 c 键集合)/template_available(lru_cache+降级空表); 缺标签测试 2 用例(破坏副本/defense 空表)"
```

---

### Task 3: generate_docx 工具

**Files:**
- Modify: `lawApp_LangGraph/tools/tools.py`(文件末尾追加)
- Test: `tests/test_docx_generation.py`(追加)

**Interfaces:**
- Consumes: Task 2 `template_path`/`load_fields`
- Produces(Task 4 依赖): LangChain tool `generate_docx(fields_json: str, doc_type: str, filename: str = "") -> dict`,返回 `{"status": "success", "docx_path": str, "filled": int, "pending": int}` 或 `{"status": "error", "message": str, "docx_path": None}`;文件名清洗 H9 正则与 markdown_to_pdf 完全一致

- [ ] **Step 1: 追加失败测试**

```python
import json as _json


def _run_tool(fields: dict, doc_type: str = "complaint", filename: str = "") -> dict:
    import asyncio
    from lawApp_LangGraph.tools.tools import generate_docx
    return asyncio.run(generate_docx.ainvoke(
        {"fields_json": _json.dumps(fields, ensure_ascii=False), "doc_type": doc_type, "filename": filename}
    ))


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
    import docx  # docxtpl 带的 python-docx
    d = docx.Document(r["docx_path"])
    text = "\n".join(p.text for p in d.paragraphs)
    assert "待补充" in text


def test_generate_docx_choice_checkboxes(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCX_OUTPUT_DIR", str(tmp_path))
    r = _run_tool({"plaintiff_gender": "男", "property_has": "有财产"})
    assert r["status"] == "success"
    import docx
    d = docx.Document(r["docx_path"])
    text = "\n".join(p.text for p in d.paragraphs)
    assert "☑男 ☐女" in text
    assert "☐无财产" in text and "☑有财产" in text


def test_generate_docx_filename_sanitized(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCX_OUTPUT_DIR", str(tmp_path))
    r = _run_tool({"plaintiff_name": "x"}, filename="../../evil/z..docx")
    assert r["status"] == "success"
    import os
    assert "evil" not in r["docx_path"].replace("\\", "/")
    assert os.path.dirname(os.path.abspath(r["docx_path"])) == str(tmp_path.resolve())


def test_generate_docx_unknown_doctype_error(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCX_OUTPUT_DIR", str(tmp_path))
    r = _run_tool({"a": "b"}, doc_type="defense")
    assert r["status"] == "error" and r["docx_path"] is None
```

- [ ] **Step 2: 跑测试确认失败**

```bash
$PY -m pytest tests/test_docx_generation.py -v
```

Expected: 新 5 用例 FAIL(ImportError: generate_docx)

- [ ] **Step 3: 实现 generate_docx(tools.py 末尾追加)**

```python
# generate_docx — docxtpl 按模板渲染 Word 文书(assistant 模式),
# 阻塞渲染放线程池执行; 字段缺失: 文本→"待补充", 勾选→全 ☐
@tool
@traced("tool")
async def generate_docx(fields_json: str, doc_type: str = "complaint", filename: str = "") -> dict:
    """按 data/doc_templates 下的模板把抽取字段渲染成 docx 文书.

    参数:
    fields_json: JSON 字符串, 键为 fields.yaml 的 key, 值为文本或选项
    doc_type: complaint(起诉状) 等模板目录名
    filename: 输出文件名(不含路径), 默认 起诉状_{时间戳}.docx

    返回:
    dict, 含 status/docx_path/filled/pending
    """
    import json as _json
    from lawApp_LangGraph.doc_templates import load_fields, template_available, template_path

    t0 = time.time()
    filename = (filename or "").strip()
    if not filename:
        filename = f"起诉状_{datetime.now().strftime('%Y%m%d_%H%M%S')}.docx"
    # H9 同款清洗: basename 防穿越 + 非白名单字符(中英文/数字/点/横杠/下划线)→下划线
    filename = re.sub(r"[^\w\-.\u4e00-\u9fff]", "_", os.path.basename(filename))
    if not filename or filename.startswith("."):
        tool_log.error("← 工具异常: generate_docx", detail=f"文件名非法: {filename!r}")
        return {"status": "error", "message": "文件名非法", "docx_path": None}

    if not template_available(doc_type):
        return {"status": "error", "message": f"模板不可用: {doc_type}", "docx_path": None}

    try:
        fields = _json.loads(fields_json) if fields_json else {}
    except Exception as e:
        return {"status": "error", "message": f"fields_json 非法: {str(e)[:120]}", "docx_path": None}

    # 构造渲染上下文: f.* 文本(缺→待补充/日期同), c.* 勾选符号
    from docxtpl import DocxTemplate
    f_ctx: dict = {}
    c_ctx: dict = {}
    filled = pending = 0
    for fd in load_fields(doc_type):
        key = fd["key"]
        if key in f.get("_MERGED_IGNORE_"):  # 占位防误用; _merge_into 字段跳过
            continue
        val = str(fields.get(key) or "").strip()
        if fd.get("_merge_into"):
            continue
        if fd["type"] == "choice":
            opts = fd.get("options") or []
            ctags = _choice_ctags(fd)  # {opt: c 键名} 从 replacement 解析
            for opt, ckey in ctags.items():
                c_ctx[ckey] = "☑" if (val and val == opt) else "☐"
            if val and val in opts:
                filled += 1
            else:
                pending += 1
        else:
            f_ctx[key] = val or "待补充"
            filled, pending = (filled + 1, pending) if val else (filled, pending + 1)

    output_dir = os.getenv("DOCX_OUTPUT_DIR", "./docx_outputs")
    os.makedirs(output_dir, exist_ok=True)
    file_path = os.path.join(output_dir, filename)

    def _render() -> None:
        tpl = DocxTemplate(template_path(doc_type))
        tpl.render({"f": f_ctx, "c": c_ctx})
        tpl.save(file_path)

    try:
        tool_log.info("→ 调用工具: generate_docx", detail=f"doc_type={doc_type} | filled={filled} | pending={pending}")
        await asyncio.to_thread(_render)
    except Exception as e:
        tool_log.error("← 工具异常: generate_docx", detail=f"渲染失败: {str(e)[:120]}")
        return {"status": "error", "message": f"docx 生成失败: {str(e)[:200]}", "docx_path": None}

    tool_log.info("← 工具返回: generate_docx", detail=f"file={filename}",
                  result=f"elapsed={time.time() - t0:.2f}s")
    return {"status": "success", "docx_path": file_path, "filled": filled, "pending": pending}


def _choice_ctags(fd: dict) -> dict:
    """从 choice 字段 YAML replacement 解析 选项→c 键名 映射。"""
    import re as _re
    repl = fd.get("replacement") or ""
    opts = fd.get("options") or []
    out = {}
    for opt in opts:
        # 形如 "{{ c.KEY }}选项" 的紧邻模式
        m = _re.search(r"\{\{\s*c\.([\w]+)\s*\}\}\s*" + _re.escape(opt), repl)
        if m:
            out[opt] = m.group(1)
    return out
```

实现注:`f_ctx`/`c_ctx` 直接以 `{{ f.xxx }}` 的 jinja 命名空间渲染——docxtpl 的 `{{ c.plaintiff_gender_male }}` 即 `context["c"]["plaintiff_gender_male"]`。上面代码里 `f.get("_MERGED_IGNORE_")` 一行是笔误占位,实现时**删掉**,只保留 `if fd.get("_merge_into"): continue`。owner 三选一手写键(如 `property_house_owner_plaintiff`)在 YAML options 里是 `choice` 且 options=[原告,被告,其他],`_choice_ctags` 从 replacement 正则解析出三个 c 键即可,`其他` 选项若 YAML replacement 写了 `{{ c.xxx_other }}` 也能命中。

- [ ] **Step 4: 跑测试确认全过**

```bash
$PY -m pytest tests/test_docx_generation.py -v
```

Expected: 12 PASS(前 7 + 新 5)

- [ ] **Step 5: Commit**

```bash
git add lawApp_LangGraph/tools/tools.py tests/test_docx_generation.py
git commit -m "C: generate_docx 工具 — docxtpl 线程池渲染; 文本缺→待补充/勾选缺→全☐; choice c 键从 YAML replacement 解析; H9 文件名清洗; 输出 DOCX_OUTPUT_DIR(默认 ./docx_outputs); 工具测试 5 用例(全填/待补充/☑☐/穿越/未知模板)"
```

---

### Task 4: 图内链路(state/prompts/executor 抽取+确认+直调/merge 落库)

**Files:**
- Modify: `lawApp_LangGraph/state.py`(AgentState 加三字段)
- Modify: `lawApp_LangGraph/LangGraph_lawApp.py`:`_STATE_KEYS` 列表(pdf_path 旁)、ingest_node 重置块(~416)、planner_node assistant 分支(~955)、executor_node(pdf_confirm 块后加 docx 分支)、merge_node(落 docx_generated)
- Modify: `lawApp_LangGraph/prompts.py`(PLANNER_ASSISTANT_SUFFIX 改写)
- Test: `tests/test_docx_generation.py`(追加)

**Interfaces:**
- Consumes: Task 2 `load_fields`/`template_available`、Task 3 `generate_docx`
- Produces(Task 5/6 依赖):
  - state 新键:`doc_fields: dict`(默认 `{}`)、`docx_path: Optional[str]`、`docx_confirmed: bool`(默认 False)
  - interrupt 载荷:`{"type": "docx_confirm", "message": str, "options": [{"value": "确认", "label": "确认生成 Word 文书"}, {"value": "跳过", "label": "跳过该步骤"}], "field_preview": [{"key","label","value","critical","status": "filled"|"pending"}]}`
  - executor 内新函数 `async def _extract_doc_fields(state, doc_type: str) -> dict`(flash LLM with_structured_output,动态 schema)
  - dialogue events 新类型 `docx_confirm` / `docx_generated`(Task 5 aggregate 依赖)

- [ ] **Step 1: state.py 加三字段(pdf_path 旁,~179 与 ~390 两处)**

```python
    # 以下为路由控制判断
    pdf_path: Optional[str] = None
    # docx 文书生成(D1-D5): 抽取字段/产物路径/确认位
    doc_fields: dict = Field(default_factory=dict)
    docx_path: Optional[str] = None
    docx_confirmed: bool = False
    error: Optional[str] = None
```

`LangGraph_lawApp.py` 顶部 `_STATE_KEYS`(含 `"pdf_path",` 的列表,~179)追加 `"doc_fields", "docx_path", "docx_confirmed"`;ingest_node 重置块(~416 `"pdf_path": None,` 旁)追加:

```python
        "doc_fields": {},
        "docx_path": None,
        "docx_confirmed": False,
```

- [ ] **Step 2: prompts.py 改写 PLANNER_ASSISTANT_SUFFIX(去类案 + docx 末步)**

```python
PLANNER_ASSISTANT_SUFFIX = """
本次是【律师助理-文书起草】任务(婚姻家事类): 用户提交了完整案件详情, 目标是起草
{doc_type_label}。规划时优先: (1)从案件详情提取文书要素(当事人/诉求/事实/证据)
(2)检索婚姻家事法条(不检索类案, 文书起草用不到) (3)评估材料缺口(缺则反问)
(4)文书结构化起草{docx_step_hint}
"""

PLANNER_ASSISTANT_DOCX_STEP = """(5)若工具列表含 generate_docx, 最后一步固定规划:
    tool_name=generate_docx, description="按起诉状模板生成 Word 文书"(参数由系统注入, 无需规划参数)。
"""
```

planner_node(~955)assistant 分支改:

```python
    if (state.mode or "attorney") == "assistant":
        from lawApp_LangGraph.doc_templates import template_available
        from lawApp_LangGraph.prompts import (
            PLANNER_ASSISTANT_SUFFIX,
            PLANNER_ASSISTANT_DOCX_STEP,
        )

        docx_step_hint = (
            PLANNER_ASSISTANT_DOCX_STEP
            if template_available(state.doc_type or "complaint")
            else "(无 Word 模板时不出 docx 步骤)"
        )
        template = PLANNER_ASSISTANT_SUFFIX.format(
            doc_type_label="起诉状" if state.doc_type != "defense" else "答辩状",
            docx_step_hint=docx_step_hint,
        )
```

`_tools_desc()`(同文件,工具描述清单)追加条目(仅描述,可用性由 planner 提示词控制):

```
- generate_docx: 按法院表格模板把案件字段渲染成 Word 文书(起诉状)。仅在工具清单标注"模板可用"时规划此步骤。
```

同时在 ALL_TOOLS()/工具注册处把 `generate_docx` 加入(看 tools.py 现有导出与 LangGraph_lawApp 的 import/注册模式,按 `markdown_to_pdf` 同样的方式接入)。

- [ ] **Step 3: executor 加 docx 分支(HITL-3 镜像,pdf_confirm 块后)**

新函数(放在 executor_node 前):

```python
async def _extract_doc_fields(state: "AgentState", doc_type: str) -> dict:
    """flash LLM 按模板字段结构化抽取: 输入案情+问诊+法条, 输出 key→值/选项。
    不可判定字段返回空串(渲染层归一为待补充/全☐); 失败抛, 由调用方走步骤 failed。
    """
    from pydantic import create_model
    from lawApp_LangGraph.doc_templates import load_fields

    fields_defs = load_fields(doc_type)
    anns = {
        fd["key"]: (str, "")
        for fd in fields_defs
        if not fd.get("_merge_into")
    }
    schema = create_model("DocFields", **anns)

    laws_digest = "\n".join(
        f"{l.law_title} {l.article_number}: {l.content[:80]}"
        for l in (state.law_results or [])[:5]
    )
    prompt = f"""你是资深婚姻家事律师助理。从下列案情中为《民事起诉状》抽取字段值。
规则: 只依据案情文本; 案情未提及的字段返回空字符串; choice 类字段必须取给定选项之一或空串。
可选选项参照(常见): 性别[男,女]; 有无财产[无财产,有财产]; 抚养归属[原告,被告]; 代理权限[一般授权,特别授权]。
"诉请依据"字段: 引用法条原文标题与条号(可参考下方检索到的法条)。

【案情】
{_query_with_supplements(state, 4000)}

【已问诊要素】
{state.case_elements.digest()}

【检索法条】
{laws_digest or '无'}
"""
    llm = get_executor_llm()  # flash 结构化输出链(_json_chain 同型)
    result = await llm.with_structured_output(schema, method="json_mode").ainvoke(
        [SystemMessage(content=prompt)]
    )
    return result.model_dump() if hasattr(result, "model_dump") else dict(result)
```

executor_node 内,`markdown_to_pdf` HITL-3 块之后追加(结构完全镜像):

```python
    # HITL-4: docx 生成前确认(镜像 pdf_confirm; 先抽取字段供预览, 确认后参数直注)
    if (
        step.tool_name == "generate_docx"
        and not state.docx_confirmed
    ):
        from lawApp_LangGraph.doc_templates import load_fields

        doc_type = state.doc_type or "complaint"
        try:
            doc_fields = await _extract_doc_fields(state, doc_type)
        except Exception as e:
            debug.warning("docx 字段抽取失败", detail=str(e)[:120])
            errored = [
                s.model_copy(update={"status": "failed", "retry_count": s.retry_count + 1})
                if i == idx else s for i, s in enumerate(plan)
            ]
            return {"plan": errored, "current_step_index": idx + 1,
                    "error": f"步骤{step.step_id} 字段抽取失败",
                    "error_streak": state.error_streak + 1,
                    "messages": [AIMessage(content="")], "docx_confirmed": True}

        fields_defs = {fd["key"]: fd for fd in load_fields(doc_type)}
        preview, critical_missing = [], []
        for k, v in doc_fields.items():
            fd = fields_defs.get(k, {})
            val = str(v or "").strip()
            if val:
                preview.append({"key": k, "label": fd.get("label", k), "value": val,
                                "critical": bool(fd.get("critical")), "status": "filled"})
            else:
                preview.append({"key": k, "label": fd.get("label", k), "value": "待补充",
                                "critical": bool(fd.get("critical")), "status": "pending"})
                if fd.get("critical"):
                    critical_missing.append(fd.get("label", k))
        n_filled = sum(1 for p in preview if p["status"] == "filled")
        n_pending = len(preview) - n_filled
        docx_msg = (
            f"即将生成 Word 文书(民事起诉状)。已填 {n_filled} 项, "
            f"待补充 {n_pending} 项"
            + (f", 关键缺失: {'、'.join(critical_missing[:5])}" if critical_missing else "")
            + "。确认生成吗?"
        )
        confirmed = interrupt(
            {
                "type": "docx_confirm",
                "message": docx_msg,
                "options": [
                    {"value": "确认", "label": "确认生成 Word 文书"},
                    {"value": "跳过", "label": "跳过该步骤"},
                ],
                "field_preview": preview,
            }
        )
        dialogue_log.log_event(
            _dialogue_sid(config),
            "docx_confirm",
            {
                "question": docx_msg,
                "chosen": "确认生成 Word 文书" if confirmed else "跳过该步骤",
                "filled": n_filled,
                "pending": n_pending,
                "critical_missing": critical_missing,
            },
        )
        if not confirmed:
            done = [
                s.model_copy(update={"status": "done"}) if i == idx else s
                for i, s in enumerate(plan)
            ]
            debug.info("← Executor docx 步骤被用户跳过", detail=f"step={idx + 1}")
            return {
                "plan": done, "current_step_index": idx + 1,
                "docx_confirmed": True, "doc_fields": doc_fields,
                "hitl_event": {"type": "docx_confirm", "confirmed": False,
                               "at": datetime.now().isoformat()},
            }
        # 确认 → 字段进 state, 落到下方直调分支
        state = state.model_copy(update={"doc_fields": doc_fields})
        confirmed_fields = doc_fields

    # generate_docx 确认后直调: 参数完全确定, 跳过 LLM 提参(镜像 analyze_legal_issue 注入)
    if step.tool_name == "generate_docx":
        sid = _dialogue_sid(config) or "session"
        tc = {
            "name": "generate_docx",
            "args": {
                "fields_json": json.dumps(confirmed_fields, ensure_ascii=False),
                "doc_type": state.doc_type or "complaint",
                "filename": f"起诉状_{re.sub(r'[^\\w\\-.\\u4e00-\\u9fff]', '_', sid)}.docx",
            },
            "id": f"docx_{idx}",
        }
        ai_msg = AIMessage(content="", tool_calls=[tc])
        doing = [
            s.model_copy(update={"status": "doing"}) if i == idx else s
            for i, s in enumerate(plan)
        ]
        return {"plan": doing, "messages": [ai_msg], "docx_confirmed": True}
```

实现注:
- `confirmed_fields` 变量在「未确认即首次进入」时未定义——上面确认分支里已赋值;当 `state.docx_confirmed` 已 True(resume 重跑 executor 整节点,如 interrupt 恢复路径从 executor 重新进入)时,直接用 `state.doc_fields`。开头补一行:`confirmed_fields = state.doc_fields or {}`,interrupt 块整体包在 `if not state.docx_confirmed:` 内,块外直调分支读 `confirmed_fields`。
- 文件名里 `re` 与 `json` 模块 executor 处已在文件头 import(核对,缺则补)。
- `dialogue_log`/`interrupt`/`AIMessage`/`datetime` 均已被 pdf 分支使用,import 沿现有。

merge_node 落 docx_generated(成功解析 output 后、`_STATE_KEYS` 回填循环之后追加):

```python
    if (
        step.tool_name == "generate_docx"
        and isinstance(output, dict)
        and output.get("docx_path")
    ):
        dialogue_log.log_event(
            _dialogue_sid(config),
            "docx_generated",
            {
                "docx_path": output["docx_path"],
                "filled": output.get("filled", 0),
                "pending": output.get("pending", 0),
            },
        )
```

- [ ] **Step 4: 追加图内流测试(真实 PG;LLM 用替身或真实按仓库测试惯例——参照 test_hitl_single_question 的图驱动用例写法)**

```python
def test_docx_confirm_interrupt_payload(tmp_path, monkeypatch):
    """executor docx 步骤: 触发 docx_confirm, 载荷含 field_preview 与二选选项。"""
    # 参照 tests/test_hitl_single_question.py 中驱动图至 interrupt 的既有夹具;
    # 构造 state: mode=assistant, doc_type=complaint, plan 末步 tool_name=generate_docx,
    # docx_confirmed=False; _extract_doc_fields 打桩返回 {"plaintiff_name": "张三"}
    ...  # 按该文件既有 interrupt 捕获模式: graph.ainvoke 后 aget_state 读 pending interrupts


def test_docx_confirm_skip_resume_no_docx(tmp_path):
    """resume 跳过: finalize 正常收尾, state 无 docx_path, 无 docx_generated 事件。"""
    ...


def test_docx_confirm_yes_resume_generates_file(tmp_path):
    """resume 确认: generate_docx 真实渲染(docxtpl), docx_path 进 state,
    docx_confirm + docx_generated 两事件落库(PG 查 session_dialogue_events)。"""
    ...
```

三条用例的骨架写法(打桩细节按 test_hitl_single_question 既有 monkeypatch/fixture 风格落地,执行者先读该文件再写;`_extract_doc_fields` 打桩点:`monkeypatch.setattr("lawApp_LangGraph.LangGraph_lawApp._extract_doc_fields", AsyncMock(return_value={...}))`。断言清单: interrupt 载荷键 type/message/options/field_preview;skip 后 `state.docx_path is None`;yes 后 `os.path.exists(state["docx_path"])` 且 PG 里 `SELECT count(*) FROM session_dialogue_events WHERE session_id=%s AND event_type='docx_generated'` ≥ 1)。

- [ ] **Step 5: 跑图内测试 + 现有回归**

```bash
$PY -m pytest tests/test_docx_generation.py tests/test_hitl_single_question.py -v
```

Expected: 全 PASS(HITL 单问回归不破)

- [ ] **Step 6: Commit**

```bash
git add lawApp_LangGraph/state.py lawApp_LangGraph/LangGraph_lawApp.py lawApp_LangGraph/prompts.py tests/test_docx_generation.py
git commit -m "C: 图内 docx 链路 — state 三件套(_STATE_KEYS+ingest 重置)/planner AS 后缀改留法条去类案+模板可用时末步固定 generate_docx/_extract_doc_fields(flash 动态 schema 抽取)/executor docx_confirm interrupt(field_preview 载荷+落库)+确认后 tool_calls 直注(仿 analyze_legal_issue)/merge docx_generated 落库; 图内流测试 3 用例(载荷/跳过/确认落盘)"
```

---

### Task 5: API 层(normalize 集合/SSE docx_done/交付端点/aggregate docx 键)

**Files:**
- Modify: `lawApp_LangGraph/FastAPI/utils.py`(normalize_resume,~216 risk/pdf 元组)
- Modify: `lawApp_LangGraph/FastAPI/api.py`:降级短词匹配元组(~205)、_mode_stream SSE updates 观察者(~600 与 ~783 两处)、新端点、QueryResponse 组装处
- Modify: `lawApp_LangGraph/FastAPI/model.py`(QueryResponse 加 docx_path)
- Modify: `lawApp_LangGraph/dialogue_log.py`(aggregate 加 docx 键)
- Test: `tests/test_docx_generation.py`(追加)

**Interfaces:**
- Consumes: Task 4 的 state `docx_path`、dialogue `docx_generated` 事件
- Produces(Task 6 依赖):
  - `docx_confirm` 进入归一化集合(确认→True/跳过→False,自由文本 LLM 语义判断)
  - SSE 新帧 `docx_done`,data=`{"path": str}`
  - `GET /sessions/{sid}/docx/latest` → FileResponse(application/vnd.openxmlformats-officedocument.wordprocessingml.document);无事件/文件丢失→404;路径越界→404
  - `GET /sessions/{sid}/dialogue` 响应新键 `docx: {path, filled, pending} | null`(最新 docx_generated 事件)

- [ ] **Step 1: 追加失败测试**

```python
def test_docx_confirm_normalize_short_words():
    from lawApp_LangGraph.FastAPI.utils import normalize_resume
    import asyncio
    assert asyncio.run(normalize_resume("docx_confirm", "确认", "生成文书?")) is True
    assert asyncio.run(normalize_resume("docx_confirm", "跳过", "生成文书?")) is False


def test_docx_latest_endpoint_404_when_none():
    import asyncio
    from lawApp_LangGraph.FastAPI.api import app
    from httpx import AsyncClient, ASGITransport
    async def run():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/api/sessions/AT-20990101-000000-999/docx/latest")
            assert r.status_code == 404
    asyncio.run(run())


def test_docx_latest_endpoint_serves_file(tmp_path):
    """真实路径: 造 session + docx_generated 事件 + 落盘文件 → 200 + 文件流。"""
    # 参照 tests/test_trace_e2e.py 的 PG 造数方式: sessions 表插 sid,
    # session_dialogue_events 插 docx_generated(path 指向 tmp 文件);
    # 断言 200、Content-Type 为 docx MIME、body 前 2 字节 == b"PK"
    ...


def test_docx_latest_rejects_path_escape(tmp_path):
    """docx_generated 事件里 path 指向 docx_outputs 外 → 404(白名单校验)。"""
    ...
```

- [ ] **Step 2: 跑测试确认失败(404 用例应在端点存在后反转)**

- [ ] **Step 3: 实现**

`utils.py` normalize_resume 两处元组 `("risk_confirm", "pdf_confirm")` → `("risk_confirm", "pdf_confirm", "docx_confirm")`(`_YES` 已含"确认",`_NO` 已含"跳过",无需改词表)。

`api.py` 降级处(~205)`if interrupt_type in ("risk_confirm", "pdf_confirm"):` 同样加 `docx_confirm`。

`api.py` _mode_stream 的 SSE updates 观察者——**两处**(GET 流 ~600 行段、resume 流 ~783 行段)各加:

```python
                        if node_name == "merge" and updates.get("docx_path"):
                            yield sse_event(
                                "docx_done", {"path": updates["docx_path"]}
                            )
```

(resume 流是 `await out_q.put((...))` 风格,改为 `await out_q.put(("docx_done", {"path": updates["docx_path"]}))`——核对 out_q 消费端是否 JSON 序列化 dict,若消费端只收 str 则 `json.dumps`;以两处现有 tool_result/elements 写法为准对齐。)

新端点(放 /ask/pdf 旁):

```python
@app.get("/sessions/{session_id}/docx/latest")
async def session_docx_latest(session_id: str):
    """会话最新 Word 文书下载(读 docx_generated 事件 → 校验路径 → FileResponse)。"""
    from lawApp_LangGraph.db import get_pool

    safe_sid = _validate_session_id(session_id)  # 复用现有 sid 格式校验 helper(名字以 api.py 实际为准)
    pool = await get_pool()
    async with pool.connection() as conn:
        row = await conn.fetchrow(
            "SELECT payload FROM session_dialogue_events "
            "WHERE session_id = %s AND event_type = 'docx_generated' "
            "ORDER BY seq DESC LIMIT 1",
            (safe_sid,),
        )
    if not row:
        raise HTTPException(status_code=404, detail="本会话尚未生成 Word 文书")
    path = (row["payload"] or {}).get("docx_path") or ""
    base = os.path.abspath(os.getenv("DOCX_OUTPUT_DIR", "./docx_outputs"))
    if not os.path.abspath(path).startswith(base + os.sep) or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="文书文件不存在或路径非法")
    return FileResponse(
        path=path,
        filename=os.path.basename(path),
        media_type=(
            "application/vnd.openxmlformats-officedocument"
            ".wordprocessingml.document"
        ),
    )
```

(执行者核对:`/sessions/{sid}/dialogue` 端点里现有的 sid 双前缀格式校验函数名并复用;fetchrow/参数占位符风格以 dialogue_log.py 的同步/异步写法为准。)

`model.py` QueryResponse 加:

```python
    # Word 文书产物路径(assistant 模式 docx 确认生成后非空)
    docx_path: Optional[str] = None
```

`api.py` 所有 QueryResponse 组装处(grep `QueryResponse(`)从 state 补 `docx_path=state.get("docx_path")`(阻塞端点;流式端点已走 docx_done 帧无需改)。

`dialogue_log.py` aggregate 函数(读 events 拼 rounds/confirms/final 处)追加:扫到 `docx_generated` 取最后一条 → 结果 dict 加 `"docx": {"path":…, "filled":…, "pending":…}`,无则 `"docx": None`。

- [ ] **Step 4: 跑测试确认全过**

```bash
$PY -m pytest tests/test_docx_generation.py -v
```

Expected: 全 PASS(约 17 条)

- [ ] **Step 5: Commit**

```bash
git add lawApp_LangGraph/FastAPI/utils.py lawApp_LangGraph/FastAPI/api.py lawApp_LangGraph/FastAPI/model.py lawApp_LangGraph/dialogue_log.py tests/test_docx_generation.py
git commit -m "C: docx API 层 — docx_confirm 入归一化集合(utils+api 降级两处)/SSE docx_done 帧挂在 merge 更新(两处流循环)/GET sessions/{sid}/docx/latest(FileResponse+docx_outputs 路径白名单+404 三态)/QueryResponse.docx_path/dialogue aggregate 加 docx 键; 端点测试 4 用例"
```

---

### Task 6: 前端(InterruptPanel 预览卡/生成中弹窗/完成 toast/下载按钮)

**Files:**
- Modify: `frontend/src/store.js`(state 加 docxGenerating/docxToast/docxPath + resetTurn 重置)
- Modify: `frontend/src/App.vue`(resumeHITL 开弹窗、handleStreamEvent 加 docx_done/error 分支、挂两新组件)
- Modify: `frontend/src/components/InterruptPanel.vue`(docx_confirm 标签/跳过值/字段预览区)
- Create: `frontend/src/components/DocxGenModal.vue`
- Create: `frontend/src/components/DocxDoneToast.vue`
- Modify: `frontend/src/components/ChatView.vue`(终答下载按钮)
- Modify: `frontend/src/components/SessionDrawer.vue`(终答节下载链接,`dialogue.docx` 存在时)

**Interfaces:**
- Consumes: Task 5 的 `docx_done` SSE 帧、`GET /api/sessions/{sid}/docx/latest`、interrupt 载荷 `field_preview`
- Produces: store 新键 `docxGenerating: bool`、`docxToast: bool`、`docxPath: str`(resetTurn 清三个,toast 组件自管 8 秒自动消失)

- [ ] **Step 1: store.js 三键**

```js
  interrupt: null,           // 当前 HITL 载荷
  docxGenerating: false,     // docx 确认后渲染中(全局弹窗)
  docxToast: false,          // docx 完成通知(左上角, 自管 8s 消失)
  docxPath: '',              // 本轮生成的文书路径(终答区下载按钮显隐)
```

resetTurn 的 Object.assign 增补 `docxGenerating: false, docxToast: false, docxPath: ''`。

- [ ] **Step 2: DocxGenModal.vue / DocxDoneToast.vue(新)**

```vue
<!-- DocxGenModal.vue -->
<script setup>
import { state } from '../store'
</script>
<template>
  <div
    v-if="state.docxGenerating"
    class="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 backdrop-blur-sm"
  >
    <div class="bg-white rounded-2xl shadow-xl px-8 py-6 flex flex-col items-center gap-3">
      <div
        class="h-8 w-8 rounded-full border-2 border-amber-500 border-t-transparent animate-spin"
      ></div>
      <div class="text-sm text-slate-700">正在生成 Word 文书…</div>
      <div class="text-xs text-slate-400">按模板填充字段, 请勿关闭页面</div>
    </div>
  </div>
</template>
```

```vue
<!-- DocxDoneToast.vue -->
<script setup>
import { onBeforeUnmount, onMounted } from 'vue'
import { state } from '../store'
let timer = null
onMounted(() => {
  timer = setTimeout(() => (state.value.docxToast = false), 8000)
})
onBeforeUnmount(() => timer && clearTimeout(timer))
</script>
<template>
  <div
    v-if="state.docxToast"
    class="fixed top-4 left-4 max-w-xs z-50 border border-amber-300 rounded-xl bg-white shadow-lg p-3 text-xs"
  >
    <div class="font-semibold text-slate-800 mb-1">Word 文书生成完成</div>
    <div class="text-slate-500 mb-2">可在消息区下载, 或点击此处下载</div>
    <a
      id="btn-download-docx"
      class="inline-block px-3 py-1 rounded bg-amber-600 text-white"
      :href="'/api/sessions/' + encodeURIComponent(state.sessionId) + '/docx/latest'"
      download
      @click="state.docxToast = false"
    >
      下载文书
    </a>
  </div>
</template>
```

toast 8 秒计时在挂载时启动——组件常驻挂 App 即可(state.docxToast 控制显隐);若用 v-if 惰性挂载由 App 控制,计时器从 watch(true) 起算,实现取常驻挂载+显隐方案(上面代码已是)。

- [ ] **Step 3: App.vue 接线**

- import 两个新组件,模板 `<DisclaimerToast />` 后加 `<DocxGenModal />` `<DocxDoneToast />`。
- `resumeHITL` 开头(savedInterrupt 取出后)加:

```js
  if (savedInterrupt?.type === 'docx_confirm' && answer === '确认') {
    state.value.docxGenerating = true
  }
```

- `handleStreamEvent` 加分支(error 分支前加 docx_done;error 分支补关弹窗):

```js
  } else if (e.event === 'docx_done') {
    state.value.docxGenerating = false
    state.value.docxToast = true
    state.value.docxPath = (e.data && e.data.path) || ''
  } else if (e.event === 'error') {
    state.value.docxGenerating = false
    ...
```

(answer 归一在后端;前端按选项 value `'确认'` 判定——InterruptPanel onConfirm 发的就是 value。)

- [ ] **Step 4: InterruptPanel.vue**

```js
// TYPE_LABEL 加:
  docx_confirm: 'Word 文书生成',
// PASS_VALUE 加:
  docx_confirm: '跳过',
```

模板问题文本下、选项列表前加字段预览区:

```html
    <!-- docx 确认: 字段预览卡(已填/待补充 两列, critical 缺失 amber 警示) -->
    <div
      v-if="interrupt.type === 'docx_confirm' && interrupt.field_preview"
      class="my-2 max-h-48 overflow-y-auto border border-slate-200 rounded-lg bg-white"
    >
      <table class="w-full text-xs">
        <tbody>
          <tr
            v-for="(fp, fi) in interrupt.field_preview"
            :id="'docx-preview-' + fi"
            :key="fp.key"
            :class="fp.status === 'pending' ? 'text-slate-400' : 'text-slate-700'"
          >
            <td class="px-2 py-0.5 border-b border-slate-100 w-32">{{ fp.label }}</td>
            <td class="px-2 py-0.5 border-b border-slate-100">
              {{ fp.value }}
              <span
                v-if="fp.status === 'pending' && fp.critical"
                class="ml-1 text-amber-600 font-semibold"
              >⚠ 待补充</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
```

- [ ] **Step 5: ChatView.vue 终答下载按钮**

消息列表末尾(messages 渲染后)追加:

```html
    <!-- docx 产物下载入口(终答后) -->
    <div v-if="state.docxPath" class="flex justify-start">
      <a
        id="btn-download-docx"
        class="inline-block px-4 py-1.5 rounded-lg bg-amber-600 text-white text-sm"
        :href="'/api/sessions/' + encodeURIComponent(state.sessionId) + '/docx/latest'"
        download
      >下载 Word 文书</a>
    </div>
```

(id `btn-download-docx` 与 toast 内按钮同名——契约要求 id 唯一,toast 内按钮改 id `btn-download-docx-toast`,本文件保持 `btn-download-docx`。)

- [ ] **Step 6: SessionDrawer.vue**

对话日志节终答区加(`dialogue.docx` 存在时,api getDialogue 新键):

```html
      <a
        v-if="dialogue.docx"
        class="inline-block px-3 py-1 rounded bg-amber-600 text-white text-xs"
        :href="'/api/sessions/' + encodeURIComponent(state.sessionId) + '/docx/latest'"
        download
      >下载 Word 文书</a>
```

(位置放最终答复节;`dialogue` 为该组件已有的 getDialogue 响应变量名,以实际为准。)

- [ ] **Step 7: 构建 + 手工冒烟**

```bash
cd frontend && npm run build
```

Expected: build 通过。起后端 + 前端 dev,assistant 模式贴案情走一轮:问诊 → 计划(末步 generate_docx)→ 确认卡(字段预览)→ 确认 → 弹窗 → toast → 下载打开验证 ☑☐/待补充 填充。跳过路径:确认卡选跳过 → 无弹窗、终答纯文本。

- [ ] **Step 8: Commit**

```bash
git add frontend/src
git commit -m "C: docx 前端 — InterruptPanel docx_confirm 分支(标签/跳过值/字段预览表 两列+critical amber 警示)/DocxGenModal 生成中全局弹窗(spinner+遮罩)/DocxDoneToast 左上角完成通知(内嵌下载, 8s 自消)/App 接线(resume 确认开弹窗, docx_done 帧收弹窗开 toast, error 关弹窗)/ChatView+SessionDrawer 下载入口(store docx 三键, resetTurn 清)/vite build 过"
```

---

### Task 7: 全量回归 + 收官

**Files:**
- 无新文件;收官提交

**Interfaces:**
- Consumes: Task 1-6 全部
- Produces: 回归绿 + spec 执行记录追加

- [ ] **Step 1: 全量 pytest**

```bash
$PY -m pytest tests/ -v
```

Expected: 全 PASS,0 FAIL(PG 停机批次按 scripts/run_pytest_timeout_skip.py 惯例 SKIP 并在提交信息注明;新 test_docx_generation.py 用例必须全绿)

- [ ] **Step 2: vite build 复跑**(Task 6 已跑,收官再确认一次)

- [ ] **Step 3: spec 追加执行记录**

`docs/superpowers/specs/2026-09-23-assistant-docx-generation-design.md` 末尾追加「执行记录」节:提交链、落地明细、测试结果、偏差存档(实现与 spec 的任何出入必须写明,如 YAML anchor 调整、docx_done 帧设计)。

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "C: docx 文书生成收官 — 全量回归 N PASS/0 FAIL + spec 执行记录(提交链/偏差存档)"
```

---

## 自审记录

- **Spec 覆盖**:D1 表格式→Task 1 模板;D2 docxtpl+YAML→Task 1/2;D3 HITL-3 镜像→Task 4;D4 一次抽取+待补充→Task 3/4;D5 预览卡→Task 4 payload+Task 6 前端;D6 留法条去类案→Task 4 prompts;D7 工具层→Task 3/4;D8 弹窗+toast→Task 6;§4 状态/交付→Task 4/5;§6 落库→Task 4/5;§7 降级→Task 2(load_fields 空)/Task 3(错误返回)/Task 4(抽取失败 failed);§8 测试→Task 1-5 分批 + Task 7 回归。无缺口。
- **占位符**:Task 4 Step 4 三条图内测试给了骨架+断言清单+参照文件,打桩/夹具细节指引执行者读 test_hitl_single_question 落地(该文件真实存在,非虚构参照);Task 5 端点测试 2 条同理。其余步骤代码完整。
- **类型一致性**:`docx_confirm`/`docx_done`/`docx_generated`/`doc_fields`/`docx_path`/`docx_confirmed`/`field_preview` 七个名字全计划统一;`generate_docx` 参数名 fields_json/doc_type/filename 与 Task 3 定义一致;`template_available`/`load_fields`/`template_path` 与 Task 2 签名一致。
