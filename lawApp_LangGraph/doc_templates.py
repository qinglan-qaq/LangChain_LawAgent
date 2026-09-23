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
    """template.docx 绝对路径字符串(生成工具按路径读取)。"""
    return str(_TEMPLATE_ROOT / doc_type / "template.docx")


def _validate_dir(dir_path: Path) -> Tuple[bool, List[str]]:
    """对账: YAML 每字段标签须出现在 template.docx(document.xml 文本)。

    - text/date 字段: {{ f.key }} 须在模板
    - choice/_merge_into 字段: 以手写 replacement 里全部 {{ c.* }} 为准, 逐键须在模板
    """
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
    """对账入口(进程内缓存); 返回 (ok, missing_tags)。"""
    ok, missing = _validate_dir(_TEMPLATE_ROOT / doc_type)
    return ok, tuple(missing)


def template_available(doc_type: str) -> bool:
    """模板可用性(进程内缓存); 对账失败记 stderr warning 并返回 False。"""
    ok, missing = validate_template(doc_type)
    if not ok:
        import sys
        print(f"[doc_templates] {doc_type} 模板不可用: {missing}", file=sys.stderr)
    return ok
