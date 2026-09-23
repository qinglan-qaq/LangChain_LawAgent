"""
Agent 工具集 — 网络搜索与 PDF 生成

工具列表:
    get_google_search   — SerpAPI 谷歌搜索,返回结构化结果(含 URL / 标题 / 摘要)
    markdown_to_pdf     — Markdown 转 PDF 文件(阻塞渲染放线程池)
    generate_docx       — docxtpl 按模板渲染 Word 文书(阻塞渲染放线程池)
"""

import asyncio
import os
import re
import time
from datetime import datetime

import markdown
from langchain_core.tools import tool

from lawApp_LangGraph.FastAPI.logging import tool as tool_log
from lawApp_LangGraph.state import WebSearchResult
from lawApp_LangGraph.tracing import traced

# get_google_search — SerpAPI 谷歌搜索, 返回标题/链接/摘要结构化结果(最多 8 条)
@tool
@traced("tool")
async def get_google_search(query: str) -> dict:
    """使用谷歌搜索API在线搜索法律相关信息.返回结构化结果,每项包含标题、链接、摘要.

    适用场景:
    - 法律案例库检索不足时,联网补充最新法规、司法解释
    - 查找特定法律条文的官方解释
    - 获取实时法律新闻和政策变动

    参数:
    query: 搜索关键词,中文或英文

    返回:
    dict,含 web_search_results 列表,每项为 WebSearchResult 实例
    """
    t0 = time.time()
    tool_log.info(
        "→ 调用工具: get_google_search",
        detail=f"query={query[:80]}",
    )

    try:
        from langchain_community.utilities import SerpAPIWrapper

        search = SerpAPIWrapper()
        raw = await asyncio.to_thread(search.results, query)
    except Exception as e:
        # M9: 异常原文不进工具结果(LLM 可见可转述, 可能含 DSN/密钥提示)——
        # 换固定中文文案, 原始异常进日志; 对齐 db_tools.fetch_laws 模式
        tool_log.error(
            "← 工具异常: get_google_search",
            detail=f"SerpAPI 不可用: {str(e)[:120]}",
            exc_info=True,
        )
        return {
            "status": "error",
            "message": "联网搜索暂时不可用,请稍后重试",
            "web_search_results": [],
        }

    structured = [
        WebSearchResult(
            title=res.get("title", ""),
            link=res.get("link", ""),
            snippet=res.get("snippet", ""),
        )
        for res in raw.get("organic_results", [])[:8]
    ]
    if not structured:
        tool_log.info(
            "← 工具返回: get_google_search",
            detail="未找到搜索结果",
            result=f"elapsed={time.time() - t0:.2f}s",
        )
        return {
            "status": "empty",
            "message": "未找到相关搜索结果",
            "web_search_results": [],
        }

    tool_log.info(
        "← 工具返回: get_google_search",
        detail=f"搜索结果{len(structured)}条",
        result=f"elapsed={time.time() - t0:.2f}s",
    )
    return {
        "status": "success",
        "count": len(structured),
        "web_search_results": structured,
    }


# PDF 导出辅助链: markdown_to_html 套样式 → _render_pdf 线程池渲染
# markdown_to_html — Markdown → HTML(启用表格/代码高亮扩展)
def markdown_to_html(markdown_text: str) -> str:
    """将Markdown文本转换为HTML字符串,并启用表格等扩展功能"""
    return markdown.markdown(markdown_text, extensions=["extra", "codehilite"])


# _render_pdf — wkhtmltopdf 同步渲染 A4 PDF(供线程池调度, 不卡事件循环)
def _render_pdf(styled_html: str, file_path: str) -> None:
    """同步渲染 PDF(wkhtmltopdf),由 asyncio.to_thread 调度。"""
    import pdfkit

    options = {
        "page-size": "A4",
        "margin-top": "0.75in",
        "margin-right": "0.75in",
        "margin-bottom": "0.75in",
        "margin-left": "0.75in",
        "encoding": "UTF-8",
        "no-outline": None,
    }
    pdfkit.from_string(styled_html, file_path, options=options)


# markdown_to_pdf — Markdown 套 A4 样式转 PDF, 阻塞渲染放线程池执行
@tool
@traced("tool")
async def markdown_to_pdf(markdown_text: str, filename: str = "") -> dict:
    """MarkDown文件转为pdf,当用户指定pdf文件输出时使用.

    参数:
    markdown_text: markdown文本内容
    filename: 输出的pdf文件名(不含路径),默认为 report_{时间戳}.pdf

    返回:
    dict,含 pdf_path 和 is_pdf_output
    """
    t0 = time.time()
    # H9: 文件名清洗 —— filename 是 LLM 可控参数, basename 防路径穿越
    # (../../evil.pdf / E:\x\evil.pdf 任意写), 非白名单字符(中英文/数字/
    # 点/横杠/下划线)统一替换为下划线
    filename = (filename or "").strip()
    if not filename:
        filename = f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    filename = re.sub(
        r"[^\w\-.\u4e00-\u9fff]", "_", os.path.basename(filename)
    )
    if not filename or filename.startswith("."):
        tool_log.error(
            "← 工具异常: markdown_to_pdf",
            detail=f"文件名非法: {filename!r}",
        )
        return {
            "status": "error",
            "message": "文件名非法",
            "pdf_path": None,
            "is_pdf_output": False,
        }

    try:
        tool_log.info(
            "→ 调用工具: markdown_to_pdf",
            detail=f"filename={filename} | content_len={len(markdown_text)}",
        )
        html_content = markdown_to_html(markdown_text)
    except Exception as e:
        tool_log.error(
            "← 工具异常: markdown_to_pdf",
            detail=f"输入非法: {str(e)[:120]}",
        )
        return {
            "status": "error",
            "message": f"PDF 生成失败: {str(e)[:200]}",
            "pdf_path": None,
            "is_pdf_output": False,
        }

    styled_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <style>
            body {{ font-family: 'PingFang SC', 'Hiragino Sans GB', 'SimHei', sans-serif; margin: 1cm; }}
            h1 {{ color: #333; }}
            code {{ font-family: monospace; background-color: #f4f4f4; }}
            pre {{ background-color: #f4f4f4; padding: 10px; border-radius: 5px; }}
        </style>
    </head>
    <body>
        {html_content}
    </body>
    </html>
    """

    output_dir = os.getenv("PDF_OUTPUT_DIR", "./pdf_outputs")
    os.makedirs(output_dir, exist_ok=True)
    file_path = os.path.join(output_dir, filename)

    try:
        # wkhtmltopdf 渲染为阻塞调用 → 线程池执行,不卡事件循环
        await asyncio.to_thread(_render_pdf, styled_html, file_path)
    except Exception as e:
        tool_log.error(
            "← 工具异常: markdown_to_pdf",
            detail=f"PDF 渲染失败: {str(e)[:120]}",
        )
        return {
            "status": "error",
            "message": f"PDF 生成失败: {str(e)[:200]}",
            "pdf_path": None,
            "is_pdf_output": False,
        }

    tool_log.info(
        "← 工具返回: markdown_to_pdf",
        detail=f"file={filename}",
        result=f"elapsed={time.time() - t0:.2f}s",
    )
    return {"status": "success", "pdf_path": file_path, "is_pdf_output": True}


# _choice_ctags — 从 choice 字段 YAML replacement 解析 选项→c 键名 映射
def _choice_ctags(fd: dict) -> dict:
    """从 choice 字段 YAML replacement 解析 选项→c 键名 映射。

    覆盖 fields.yaml 的实际写法:
    - 紧邻式 "{{ c.KEY }}选项"(性别/有无等多数字段);
    - 分隔式 "{{ c.KEY }}/被告" 与短引语前缀 "{{ c.KEY }}已经诉前保全"
      (c 键与选项文本之间允许 ≤3 个非换行/非花括号字符);
    - 仍有未命中选项且剩余 c 键数与之相等时按出现顺序配对兜底
      (preservation 的"无"在 replacement 里写作"否")。
    """
    repl = fd.get("replacement") or ""
    opts = fd.get("options") or []
    ckeys = re.findall(r"\{\{\s*c\.([A-Za-z0-9_]+)\s*\}\}", repl)
    out: dict = {}
    for opt in opts:
        m = re.search(
            r"\{\{\s*c\.([A-Za-z0-9_]+)\s*\}\}[^\n{}]{0,3}" + re.escape(opt), repl
        )
        if m and m.group(1) not in out.values():
            out[opt] = m.group(1)
    unmatched = [o for o in opts if o not in out]
    rest = [k for k in ckeys if k not in out.values()]
    if unmatched and len(unmatched) == len(rest):
        out.update(zip(unmatched, rest))
    return out


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

    from lawApp_LangGraph.doc_templates import (
        load_fields,
        template_available,
        template_path,
    )

    t0 = time.time()
    filename = (filename or "").strip()
    if not filename:
        filename = f"起诉状_{datetime.now().strftime('%Y%m%d_%H%M%S')}.docx"
    # H9 同款清洗: basename 防穿越 + 非白名单字符(中英文/数字/点/横杠/下划线)→下划线
    filename = re.sub(r"[^\w\-.\u4e00-\u9fff]", "_", os.path.basename(filename))
    if not filename or filename.startswith("."):
        tool_log.error(
            "← 工具异常: generate_docx",
            detail=f"文件名非法: {filename!r}",
        )
        return {"status": "error", "message": "文件名非法", "docx_path": None}

    if not template_available(doc_type):
        return {"status": "error", "message": f"模板不可用: {doc_type}", "docx_path": None}

    try:
        fields = _json.loads(fields_json) if fields_json else {}
    except Exception as e:
        return {"status": "error", "message": f"fields_json 非法: {str(e)[:120]}", "docx_path": None}

    # 构造渲染上下文: f.* 文本(缺→"待补充", date 同), c.* 勾选符号;
    # _merge_into 附带字段随宿主 replacement, 不单独计数
    all_fields = load_fields(doc_type)
    f_ctx: dict = {}
    c_ctx: dict = {}
    filled = pending = 0
    for fd in all_fields:
        key = fd["key"]
        val = str(fields.get(key) or "").strip()
        if fd["type"] == "choice":
            for opt, ckey in _choice_ctags(fd).items():
                c_ctx[ckey] = "☑" if (val and val == opt) else "☐"
        else:
            # _merge_into 附带字段同路径渲染(其 {{ f.KEY }} 标签写在宿主 replacement
            # 内), 缺失同样给"待补充"(spec D4); 仅下方计数跳过(挂在宿主那一项)
            f_ctx[key] = val or "待补充"
        if fd.get("_merge_into"):
            continue
        # 计数口径: 每个非 _merge_into 字段算一项, 值非空=filled, 空=pending
        if val:
            filled += 1
        else:
            pending += 1
    # 未被 choice 命中的手写勾选键(text 字段 replacement 内的归属三选一等)统一缺省 ☐
    for fd in all_fields:
        for ckey in re.findall(r"\{\{\s*c\.([A-Za-z0-9_]+)\s*\}\}", fd.get("replacement") or ""):
            c_ctx.setdefault(ckey, "☐")

    output_dir = os.getenv("DOCX_OUTPUT_DIR", "./docx_outputs")
    os.makedirs(output_dir, exist_ok=True)
    file_path = os.path.join(output_dir, filename)

    def _render() -> None:
        from docxtpl import DocxTemplate

        tpl = DocxTemplate(template_path(doc_type))
        # autoescape=True: 字段值含 &/< 等 XML 特殊字符时转实体渲染, 防打崩 XML
        # 解析(渲染失败), 亦防良构标签注入文档结构; Word 显示仍为原字符
        tpl.render({"f": f_ctx, "c": c_ctx}, autoescape=True)
        tpl.save(file_path)

    try:
        tool_log.info(
            "→ 调用工具: generate_docx",
            detail=f"doc_type={doc_type} | filled={filled} | pending={pending}",
        )
        await asyncio.to_thread(_render)
    except Exception as e:
        tool_log.error(
            "← 工具异常: generate_docx",
            detail=f"渲染失败: {str(e)[:120]}",
        )
        return {"status": "error", "message": f"docx 生成失败: {str(e)[:200]}", "docx_path": None}

    tool_log.info(
        "← 工具返回: generate_docx",
        detail=f"file={filename}",
        result=f"elapsed={time.time() - t0:.2f}s",
    )
    return {"status": "success", "docx_path": file_path, "filled": filled, "pending": pending}
