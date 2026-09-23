"""docx 模板构建脚本 — 按字段结构定义把法院源表 docx 转成 docxtpl 模板。

把 source.docx(法院表格式《离婚民事起诉状》)按 fields.yaml 的 anchor 正则逐字段定位,
将原文替换为 {{ f.KEY }}(文本/日期字段)与 {{ c.KEY_OPT }}(勾选字段)标签, 产出
template.docx 供 docxtpl 渲染(勾选值由渲染层给 ☑/☐, 缺失文本给"待补充")。

用法:
    python scripts/build_docx_template.py \
        --src data/doc_templates/complaint/source.docx \
        --yaml data/doc_templates/complaint/fields.yaml \
        --out data/doc_templates/complaint/template.docx

约束:
- 每个 anchor×occurrence 必须命中, 未命中即抛 ValueError 非零退出
  (构建期暴露, 不留静默缺口);
- text 模式定位基于"段落可见文本"(段落内所有 w:t 跨 run 合并)——Word 常把一行
  拆成多个 w:t, 直接对原始 XML 做文本正则会漏匹配(如"性别：男 女"拆成两个 run);
- occurrence 计数一律基于原始冻结文本, 与字段处理顺序解耦(先替换的字段不再影响
  后续字段的命中序号);
- replacement 中的换行表示"锚点段之后逐段落覆盖": 首行替换锚点段内命中文本,
  其余各行依序整段覆盖锚点段之后的连续段落(w:t 不接受换行, 拆段实现);
- 产出前对账: 所有手写 {{ c.* }} 与全部 {{ f.* }} 标签必须落在模板内, 缺一即构建失败。
"""

import argparse
import re
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import yaml

# 段落/文本抽取正则(w:p 不嵌套; 自闭合形态放在前, 防止其被普通形态吞并)
P_RE = re.compile(r"<w:p\b[^>]*?/>|<w:p\b[^>]*?>.*?</w:p>", re.S)
WT_RE = re.compile(r"<w:t[^>]*>(.*?)</w:t>", re.S)
PPR_RE = re.compile(r"<w:pPr>.*?</w:pPr>", re.S)
RUN_RE = re.compile(r"<w:r\b[^>]*>.*?</w:r>", re.S)
RPR_RE = re.compile(r"<w:rPr>.*?</w:rPr>", re.S)

# 兜底 run 属性: 宋体 / 18 半磅(9pt), 与源文书正文一致
DEFAULT_RPR = (
    '<w:rFonts w:hint="eastAsia" w:ascii="宋体" w:hAnsi="宋体" w:cs="宋体"/>'
    '<w:color w:val="000000"/><w:sz w:val="18"/><w:szCs w:val="18"/>'
)


def _unescape(text: str) -> str:
    """还原 w:t 内的 XML 实体(&amp; 必须最后处理)。"""
    return (
        text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
        .replace("&amp;", "&")
    )


class Paragraph:
    """document.xml 中的一个 <w:p> 段落: 记录原文 span 与解析出的结构部件。"""

    def __init__(self, start: int, end: int, tag: str):
        self.start, self.end, self.tag = start, end, tag
        self.self_closed = tag.endswith("/>")
        # 开标签(不含结尾 ">"), 保留 w14:paraId 等属性
        self.open_tag = tag[:-2] if self.self_closed else tag[: tag.index(">")]
        m = PPR_RE.search(tag)
        self.ppr = m.group(0) if m else ""
        # run 属性优先取首个 run 的 rPr(字体/字号随正文); 无 run 时退回 pPr 段落标记属性
        run = RUN_RE.search(tag)
        rpr = RPR_RE.search(run.group(0)) if run else (RPR_RE.search(self.ppr) if self.ppr else None)
        self.run_rpr = rpr.group(0) if rpr else ""
        self.text = _unescape("".join(WT_RE.findall(tag)))

    def rebuild(self, new_text: str) -> str:
        """用 new_text 重建段落: 保留开标签属性与 pPr, 正文收敛为单 run 单 w:t。"""
        body = self.ppr
        if new_text:
            rpr = self.run_rpr or f"<w:rPr>{DEFAULT_RPR}</w:rPr>"
            body += f'<w:r>{rpr}<w:t xml:space="preserve">{escape(new_text)}</w:t></w:r>'
        return f"{self.open_tag}>{body}</w:p>"


def replace_nth_xml(xml: str, pattern: str, replacement: str, occurrence: int, key: str) -> str:
    """xml 模式: 替换 pattern 的第 occurrence 次匹配(0 基); 找不够即报错。"""
    hits = list(re.finditer(pattern, xml))
    if len(hits) <= occurrence:
        raise ValueError(
            f"字段 {key} anchor 命中不足: {pattern!r} 需要第 {occurrence} 处, 实际 {len(hits)} 处"
        )
    m = hits[occurrence]
    return xml[: m.start()] + replacement + xml[m.end():]


def find_hit(paras: list, pattern: str, occurrence: int, key: str):
    """text 模式: 在冻结的段落可见文本流上取 pattern 的第 occurrence 次命中。"""
    hits = [(i, m) for i, p in enumerate(paras) for m in re.finditer(pattern, p.text)]
    if len(hits) <= occurrence:
        raise ValueError(
            f"字段 {key} anchor 命中不足: {pattern!r} 需要第 {occurrence} 处, 实际 {len(hits)} 处"
        )
    return hits[occurrence]


def add_span_edit(edits: dict, tail_texts: dict, idx: int, s: int, e: int, repl: str, key: str) -> None:
    """登记一处段内区间替换; 与尾段覆盖/既有区间重叠即报错(不允许静默互踩)。"""
    if idx in tail_texts:
        raise ValueError(f"字段 {key} 命中段落 #{idx}, 但该段已被多段尾段覆盖")
    for s0, e0, _ in edits.get(idx, []):
        if not (e <= s0 or e0 <= s):
            raise ValueError(f"字段 {key} 与既有编辑在段落 #{idx} 区间重叠")
    edits.setdefault(idx, []).append((s, e, repl))


def build_template(xml: str, spec: dict) -> str:
    """对 document.xml 执行全部字段替换, 返回新 XML 字符串。"""
    # 1) xml 模式字段: 直接在原始 XML 上按序替换
    for f in spec["fields"]:
        anchor = f.get("anchor")
        if not anchor or f.get("anchor_mode") != "xml":
            continue
        xml = replace_nth_xml(xml, anchor, f["replacement"], f.get("occurrence", 0), f["key"])

    # 2) 解析段落, 冻结原始可见文本(occurrence 计数与字段顺序解耦的关键)
    paras = [Paragraph(m.start(), m.end(), m.group(0)) for m in P_RE.finditer(xml)]
    edits = {}       # 段下标 -> [(start, end, 替换文本)]
    tail_texts = {}  # 段下标 -> 整段新文本(多段 replacement 尾段落)

    for f in spec["fields"]:
        anchor = f.get("anchor")
        if not anchor or f.get("anchor_mode") == "xml":
            continue  # _merge_into 附带字段无独立锚点; xml 模式已在上面处理
        key, occ = f["key"], f.get("occurrence", 0)
        idx, m = find_hit(paras, anchor, occ, key)
        if "replacement" in f:
            repl = f["replacement"]
        elif f.get("type") == "choice":
            raise ValueError(f"choice 字段 {key} 必须手写 replacement")
        else:
            # 缺省: 命中文本原样保留, 追加标签
            repl = m.group(0) + "{{ f." + key + " }}"
        if "\n" not in repl:
            add_span_edit(edits, tail_texts, idx, m.start(), m.end(), repl, key)
        else:
            # 多段: 首行替换锚点段内命中文本, 其余各行依序整段覆盖后续段落
            parts = repl.split("\n")
            add_span_edit(edits, tail_texts, idx, m.start(), m.end(), parts[0], key)
            for k, part in enumerate(parts[1:], start=1):
                t_idx = idx + k
                if t_idx >= len(paras):
                    raise ValueError(f"字段 {key} 尾段越界: 段落 #{t_idx} 不存在")
                if t_idx in edits or t_idx in tail_texts:
                    raise ValueError(f"字段 {key} 尾段与既有编辑冲突: 段落 #{t_idx}")
                tail_texts[t_idx] = part

    # 3) 倒序重建被编辑段落(按原文 span 从后往前替换, 保证前序 span 不失效)
    for i in sorted(set(edits) | set(tail_texts), key=lambda i: paras[i].start, reverse=True):
        p = paras[i]
        if i in tail_texts:
            new_tag = p.rebuild(tail_texts[i])
        else:
            pieces, pos = [], 0
            for s, e, repl in sorted(edits[i]):
                pieces += [p.text[pos:s], repl]
                pos = e
            pieces.append(p.text[pos:])
            new_tag = p.rebuild("".join(pieces))
        xml = xml[: p.start] + new_tag + xml[p.end:]
    return xml


def reconcile(xml: str, spec: dict) -> None:
    """对账: YAML 手写的每个 {{ c.* }} 与每个字段的 {{ f.* }} 标签必须落在产物里。"""
    problems = []
    for f in spec["fields"]:
        for ckey in re.findall(r"\{\{\s*c\.([A-Za-z0-9_]+)\s*\}\}", f.get("replacement", "")):
            if "{{ c." + ckey + " }}" not in xml:
                problems.append(f"缺勾选标签 {{{{ c.{ckey} }}}} (字段 {f['key']})")
        if f.get("type") in ("text", "date") and "{{ f." + f["key"] + " }}" not in xml:
            problems.append(f"缺文本标签 {{{{ f.{f['key']} }}}}")
        if f.get("type") == "choice" and not re.search(
            r"\{\{ c\." + re.escape(f["key"]) + r"_[A-Za-z0-9_]+ \}\}", xml
        ):
            problems.append(f"choice 字段 {f['key']} 无任何勾选标签")
    if problems:
        raise ValueError("对账失败:\n" + "\n".join(problems))


def main() -> None:
    """入口: 读源 docx 与 fields.yaml, 产出 template.docx。"""
    ap = argparse.ArgumentParser(description="按 fields.yaml 把 source.docx 转成 docxtpl 模板")
    ap.add_argument("--src", required=True, help="源 docx 路径")
    ap.add_argument("--yaml", required=True, help="fields.yaml 路径")
    ap.add_argument("--out", required=True, help="产出 template.docx 路径")
    args = ap.parse_args()

    spec = yaml.safe_load(Path(args.yaml).read_text(encoding="utf-8"))
    with zipfile.ZipFile(args.src) as zin:
        xml = zin.read("word/document.xml").decode("utf-8")
        new_xml = build_template(xml, spec)
        reconcile(new_xml, spec)
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        # 复制源 zip, 仅替换 document.xml(样式表/编号等原样保留)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == "word/document.xml":
                    data = new_xml.encode("utf-8")
                zout.writestr(item, data)

    n_f = len(re.findall(r"\{\{ f\.[A-Za-z0-9_]+ \}\}", new_xml))
    n_c = len(re.findall(r"\{\{ c\.[A-Za-z0-9_]+ \}\}", new_xml))
    print(f"模板已生成: {out} (f 标签 {n_f} 个, c 标签 {n_c} 个, 字段 {len(spec['fields'])} 条)")


if __name__ == "__main__":
    main()
