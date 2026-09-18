"""案例语料批量入库 law_cases(pgvector)。

用法: $PY scripts/ingest_cases_pgvector.py [--dry-run]
幂等: 以 id = f"{文件名}::{chunk_index}" 为主键, 重跑覆盖同 id 行(ON CONFLICT DO UPDATE)。
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHUNK = 500
OVERLAP = 50


def _chunks(text: str) -> list[str]:
    """滑动窗口分块: 500 字/块, 50 字重叠。"""
    step = CHUNK - OVERLAP
    return [text[i : i + CHUNK] for i in range(0, max(len(text) - OVERLAP, 1), step)]


def _parse_meta(path: Path) -> tuple[str, str]:
    """从文件名提取 (year, case_number)。文件名形如 中国法院2014年度案例_婚姻家庭与继承纠纷.md。"""
    m = re.search(r"(19|20)\d{2}", path.stem)
    return (m.group(0) if m else "", path.stem)


async def main(dry: bool = False) -> None:
    """读取 data/Documents/MarkDownFiles 全部案例 md,分块嵌入后写入 law_cases。"""
    from lawApp_LangGraph.RAG_service.embedder import embed_documents_sync
    from lawApp_LangGraph.db import get_pool

    docs_dir = ROOT / "data" / "Documents" / "MarkDownFiles"
    files = sorted(docs_dir.glob("*.md"))
    if not files:
        raise SystemExit(f"未找到案例语料: {docs_dir}")

    prepared: list[tuple] = []
    for f in files:
        year, case_number = _parse_meta(f)
        text = re.sub(r"\s+", " ", f.read_text(encoding="utf-8")).strip()
        for i, chunk in enumerate(_chunks(text)):
            prepared.append((f"{f.stem}::{i}", year, case_number, "", i, chunk))
    if dry:
        print(f"[dry-run] {len(files)} 个文件 → {len(prepared)} 块")
        return

    texts = [p[5] for p in prepared]
    vectors: list[list[float]] = []
    SLICE = 256
    for i in range(0, len(texts), SLICE):
        vectors.extend(embed_documents_sync(texts[i : i + SLICE]))
        print(f"嵌入进度: {min(i + SLICE, len(texts))}/{len(texts)}", flush=True)
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            INSERT INTO law_cases (id, year, case_number, case_cause, chunk_index, chunk_text, embedding)
            VALUES (%s, %s, %s, %s, %s, %s, %s::vector)
            ON CONFLICT (id) DO UPDATE SET chunk_text = EXCLUDED.chunk_text,
                                           embedding = EXCLUDED.embedding
            """,
            [(p[0], p[1], p[2], p[3], p[4], p[5], v) for p, v in zip(prepared, vectors)],
        )
        await conn.commit()
        print(f"入库完成: {cur.rowcount} 行 (来自 {len(files)} 个文件)")


if __name__ == "__main__":
    # psycopg_async 需 SelectorEventLoop; 必须在 asyncio.run 创建循环前设置
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main(dry="--dry-run" in sys.argv))
