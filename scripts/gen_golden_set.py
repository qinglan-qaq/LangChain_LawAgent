"""生成 retrieval golden set(默认 30 条): Pinecone 命名空间全量拉取 →
chunk_index=0 过滤 → 按案由分层抽样 → DeepSeek 从 chunk_text 提炼当事人
视角问题。ground truth = 该案例全部 chunk 的向量 id(relevant_ids), hit 与
P/R/F1 因此有真实意义。产出 data/eval/retrieval.jsonl(不进 git, 可重生成)。

用法: python scripts/gen_golden_set.py [--count 30] [--out data/eval/retrieval.jsonl]
"""
import argparse
import json
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def _index():
    from lawApp_LangGraph.config import settings
    from lawApp_LangGraph.RAG_service.pinecone_retriever import _get_service

    svc = _get_service()
    return svc.pc.Index(settings.pinecone_index_name), settings.pinecone_namespace


def fetch_all_vectors() -> list[dict]:
    """命名空间全量 id 列表 + 元数据(分批 fetch)。"""
    index, ns = _index()
    ids: list[str] = []
    token = None
    while True:
        resp = index.list_paginated(namespace=ns, limit=100,
                                     pagination_token=token)
        ids.extend(v.id for v in resp.vectors)
        token = resp.pagination.next if resp.pagination else None
        if not token:
            break
    if not ids:
        raise SystemExit("Pinecone 命名空间为空, 无法生成 golden set")

    vecs: list[dict] = []
    for i in range(0, len(ids), 90):
        fr = index.fetch(ids=ids[i:i + 90], namespace=ns)
        for vid, vec in (fr.vectors or {}).items():
            meta = vec.metadata or {}
            vecs.append({"vid": vid, **meta})
    return vecs


def sample_cases(count: int) -> list[dict]:
    vecs = fetch_all_vectors()
    # 同案例全部 chunk 归组(relevant_ids), 抽样只取首块
    by_case: dict[str, dict] = {}
    for v in vecs:
        key = v.get("case_number") or v["vid"]
        g = by_case.setdefault(key, {"case_id": key, "case_cause":
                                     v.get("case_cause") or "通用",
                                     "year": v.get("year", ""),
                                     "chunks": []})
        g["chunks"].append(v["vid"])
        # 首块(chunk_index=0)案情概览最完整, 无则取首个有文本的块
        if v.get("chunk_text") and (v.get("chunk_index") == 0
                                    or "chunk_text" not in g):
            g["chunk_text"] = v["chunk_text"]
    cases = [c for c in by_case.values() if c.get("chunk_text")]
    # 按案由分层轮转抽
    by_cause: dict[str, list] = {}
    for c in cases:
        by_cause.setdefault(c["case_cause"], []).append(c)
    rng = random.Random(42)
    picked: list[dict] = []
    groups = [list(v) for v in by_cause.values()]
    while len(picked) < count and groups:
        for g in groups:
            if g and len(picked) < count:
                picked.append(g.pop(rng.randrange(len(g))))
        groups = [g for g in groups if g]
    return picked


def refine_question(chunk_text: str) -> str:
    """DeepSeek 从 chunk 提炼当事人视角问题; 失败回退截断原文。"""
    import httpx

    from lawApp_LangGraph.config import settings

    prompt = (
        "你是测试用例设计者。以下是一段婚姻家事案例原文, 请以当事人第一视角"
        "提炼一个自然语言法律咨询问题(50字内, 不复述案情细节, 只留诉求), "
        "只输出问题本身:\n\n" + chunk_text[:1200]
    )
    try:
        resp = httpx.post(
            f"{settings.deepseek_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
            json={"model": settings.deepseek_flash_model,
                  "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": 120, "temperature": 0.3},
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return chunk_text[:80] + "...(请分析此案例涉及的法律问题)"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=30)
    ap.add_argument("--out", default=str(REPO / "data" / "eval" / "retrieval.jsonl"))
    args = ap.parse_args()

    picked = sample_cases(args.count)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for c in picked:
            f.write(json.dumps({
                "case_id": c["case_id"],
                "question": refine_question(c["chunk_text"]),
                "case_cause": c["case_cause"],
                "year": c["year"],
                "relevant_ids": sorted(c["chunks"]),
                "chunk_text": c["chunk_text"][:1500],
            }, ensure_ascii=False) + "\n")
    print(f"golden set 生成完成: {len(picked)} 条 → {out_path}")


if __name__ == "__main__":
    main()
