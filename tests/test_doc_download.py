"""doc/{format} 双格式下载端点 — docx 别名等价 / pdf 按需转换+缓存 / 白名单复用。"""
import asyncio
import os
import sys
from pathlib import Path

if sys.platform == "win32":
    # psycopg async 在 Windows 需 SelectorEventLoop(对齐 test_dialogue_events)
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

_T_PREFIX = "T-doc-dl-test"


def _pg_ok() -> bool:
    """独立短连接探测 PG(不碰全局池, 对齐 test_dialogue_events)。"""

    async def _probe():
        from psycopg import AsyncConnection

        from lawApp_LangGraph.db import build_dsn

        conn = await AsyncConnection.connect(build_dsn(), autocommit=True)
        try:
            await conn.execute("SELECT 1")
        finally:
            await conn.close()

    try:
        asyncio.run(_probe())
        return True
    except Exception:
        return False


def _skip_if_no_pg():
    if not _pg_ok():
        import pytest

        pytest.skip("PG 不可用, 显式跳过(不 mock)")


def _docx_cleanup() -> None:
    """清理测试会话事件行(独立短连接, 前缀为本文件的 _T_PREFIX)。"""

    async def _run():
        from psycopg import AsyncConnection

        from lawApp_LangGraph.db import build_dsn

        conn = await AsyncConnection.connect(build_dsn(), autocommit=True)
        try:
            await conn.execute(
                "DELETE FROM session_dialogue_events WHERE session_id LIKE %s",
                (_T_PREFIX + "%",),
            )
        finally:
            await conn.close()

    try:
        asyncio.run(_run())
    except Exception:
        pass  # PG 不可用 → 后续用例各自 SKIP


def _mk_docx_event(tmp_path, sid, name="起诉状_test.docx"):
    """落盘假 docx + 注入 docx_generated 事件, 返回文件路径。"""
    from lawApp_LangGraph import dialogue_log

    f = tmp_path / name
    f.write_bytes(b"PK\x03\x04fake-docx-payload")
    dialogue_log.log_event(sid, "docx_generated",
                           {"docx_path": str(f), "filled": 6, "pending": 1})
    return f


def test_docx_alias_equivalent(tmp_path, monkeypatch):
    """/doc/docx 与旧 /docx/latest 返回同一文件。"""
    _skip_if_no_pg()
    _docx_cleanup()
    monkeypatch.setenv("DOCX_OUTPUT_DIR", str(tmp_path))
    sid = _T_PREFIX + "-alias"
    _mk_docx_event(tmp_path, sid)
    try:
        from fastapi.testclient import TestClient

        from lawApp_LangGraph.FastAPI.api import app

        with TestClient(app) as client:
            r1 = client.get(f"/sessions/{sid}/doc/docx")
            r2 = client.get(f"/sessions/{sid}/docx/latest")
            assert r1.status_code == 200 and r2.status_code == 200
            assert r1.content == r2.content == b"PK\x03\x04fake-docx-payload"
    finally:
        _docx_cleanup()


def test_pdf_converted_and_cached(tmp_path, monkeypatch):
    """format=pdf: 转换落 PDF_OUTPUT_DIR + application/pdf; 二次请求命中缓存不重转。"""
    _skip_if_no_pg()
    _docx_cleanup()
    monkeypatch.setenv("DOCX_OUTPUT_DIR", str(tmp_path))
    pdf_dir = tmp_path / "pdfs"
    monkeypatch.setenv("PDF_OUTPUT_DIR", str(pdf_dir))
    sid = _T_PREFIX + "-pdf"
    _mk_docx_event(tmp_path, sid)
    calls = {"n": 0}

    def _fake_convert(src, dst):
        calls["n"] += 1
        Path(dst).write_bytes(b"%PDF-1.4-fake")

    import lawApp_LangGraph.FastAPI.api as api
    monkeypatch.setattr(api, "_convert_docx_pdf", _fake_convert)
    try:
        from fastapi.testclient import TestClient

        from lawApp_LangGraph.FastAPI.api import app

        with TestClient(app) as client:
            r1 = client.get(f"/sessions/{sid}/doc/pdf")
            assert r1.status_code == 200, r1.text
            assert r1.headers["content-type"] == "application/pdf"
            assert (pdf_dir / "起诉状_test.pdf").exists()
            r2 = client.get(f"/sessions/{sid}/doc/pdf")
            assert r2.status_code == 200
        assert calls["n"] == 1, "缓存命中不应二次转换"
    finally:
        _docx_cleanup()


def test_pdf_stale_cache_reconverts(tmp_path, monkeypatch):
    """缓存 pdf mtime 旧于 docx → 重新转换。"""
    _skip_if_no_pg()
    _docx_cleanup()
    monkeypatch.setenv("DOCX_OUTPUT_DIR", str(tmp_path))
    pdf_dir = tmp_path / "pdfs"
    monkeypatch.setenv("PDF_OUTPUT_DIR", str(pdf_dir))
    sid = _T_PREFIX + "-stale"
    f = _mk_docx_event(tmp_path, sid)
    stale = pdf_dir / "起诉状_test.pdf"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"%PDF-old")
    import time

    time.sleep(0.05)
    os.utime(f, None)  # docx 比缓存新
    calls = {"n": 0}

    def _fake_convert(src, dst):
        calls["n"] += 1
        Path(dst).write_bytes(b"%PDF-new")

    import lawApp_LangGraph.FastAPI.api as api
    monkeypatch.setattr(api, "_convert_docx_pdf", _fake_convert)
    try:
        from fastapi.testclient import TestClient

        from lawApp_LangGraph.FastAPI.api import app

        with TestClient(app) as client:
            r = client.get(f"/sessions/{sid}/doc/pdf")
            assert r.status_code == 200 and r.content == b"%PDF-new"
        assert calls["n"] == 1
    finally:
        _docx_cleanup()


def test_pdf_conversion_failure_502(tmp_path, monkeypatch):
    """转换抛错 → 502, detail 含 Word 提示; docx 下载不受影响。"""
    _skip_if_no_pg()
    _docx_cleanup()
    monkeypatch.setenv("DOCX_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("PDF_OUTPUT_DIR", str(tmp_path / "pdfs"))
    sid = _T_PREFIX + "-502"
    _mk_docx_event(tmp_path, sid)

    def _boom(src, dst):
        raise RuntimeError("COM unavailable")

    import lawApp_LangGraph.FastAPI.api as api
    monkeypatch.setattr(api, "_convert_docx_pdf", _boom)
    try:
        from fastapi.testclient import TestClient

        from lawApp_LangGraph.FastAPI.api import app

        with TestClient(app) as client:
            r = client.get(f"/sessions/{sid}/doc/pdf")
            assert r.status_code == 502
            assert "Word" in r.json()["detail"]
            assert client.get(f"/sessions/{sid}/doc/docx").status_code == 200
    finally:
        _docx_cleanup()


def test_doc_format_invalid_400(tmp_path, monkeypatch):
    """format=exe → 400 invalid_format(不触文件查找)。"""
    monkeypatch.setenv("DOCX_OUTPUT_DIR", str(tmp_path))
    from fastapi.testclient import TestClient

    from lawApp_LangGraph.FastAPI.api import app

    with TestClient(app) as client:
        r = client.get(f"/sessions/AT-20990101-000000-999/doc/exe")
        assert r.status_code == 400
        assert r.json()["detail"] == "invalid_format"


# ---- Fix round 1: 失败残留清理 + 超时分支消息 ----


def test_pdf_failure_cleans_partial_then_retry(tmp_path, monkeypatch):
    """转换中途抛错时半成品 pdf 必须被清理 — 否则其新 mtime 会命中缓存,
    后续请求 200 返回坏文件; 清理后重试走真实转换(直调 _docx_to_pdf, 无 PG 依赖)。"""
    import pytest
    from fastapi import HTTPException

    pdf_dir = tmp_path / "pdfs"
    monkeypatch.setenv("PDF_OUTPUT_DIR", str(pdf_dir))
    f = tmp_path / "起诉状_test.docx"
    f.write_bytes(b"PK\x03\x04fake-docx-payload")
    calls = {"n": 0}

    def _partial_then_boom(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            Path(dst).write_bytes(b"%PDF-partial-broken")
            raise RuntimeError("COM unavailable")
        Path(dst).write_bytes(b"%PDF-ok")

    import lawApp_LangGraph.FastAPI.api as api
    monkeypatch.setattr(api, "_convert_docx_pdf", _partial_then_boom)

    pdf = pdf_dir / "起诉状_test.pdf"
    with pytest.raises(HTTPException) as ei:
        asyncio.run(api._docx_to_pdf(str(f)))
    assert ei.value.status_code == 502
    assert "Word" in ei.value.detail
    assert not pdf.exists(), "半成品 pdf 必须清理, 不得残留污染 mtime 缓存"

    out = asyncio.run(api._docx_to_pdf(str(f)))
    assert out == str(pdf)
    assert pdf.read_bytes() == b"%PDF-ok", "重试不得返回半成品内容"
    assert calls["n"] == 2, "清理后缓存未命中, 应重新转换"


def test_pdf_timeout_detail_message(tmp_path, monkeypatch):
    """超时分支 → detail 'PDF 转换超时(30s)', 不误报缺 Word(直调, 无 PG 依赖)。"""
    import pytest
    from fastapi import HTTPException

    monkeypatch.setenv("PDF_OUTPUT_DIR", str(tmp_path / "pdfs"))
    f = tmp_path / "起诉状_test.docx"
    f.write_bytes(b"PK\x03\x04fake-docx-payload")

    def _slow(src, dst):
        raise TimeoutError()

    import lawApp_LangGraph.FastAPI.api as api
    monkeypatch.setattr(api, "_convert_docx_pdf", _slow)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(api._docx_to_pdf(str(f)))
    assert ei.value.status_code == 502
    assert ei.value.detail == "PDF 转换超时(30s)"
