"""评测 runner(P2): retrieval suite 直调检索器(不走 HTTP/图), 每用例取
top-10 排序 id, 指标聚合落 eval_runs; --baseline 输出两批 diff。
calibration suite 离线标定三档阈值(正/负 sigmoid 分布 → 建议切点, 不落库)。

用法: python scripts/run_eval.py --suite retrieval --label <git_sha> [--baseline <label>]
      python scripts/run_eval.py --suite calibration --label <标定标签>
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


async def run_retrieval(label: str, baseline: str | None) -> None:
    from lawApp_LangGraph import db
    from lawApp_LangGraph.eval_metrics import evaluate
    # golden set 的 ranked id 空间 = Pinecone vector id, 必须绑定 Pinecone 后端
    # (get_retriever 读 os.environ, 脚本进程无 .env 注入, 会错落到 pgvector)
    from lawApp_LangGraph.RAG_service.pinecone_retriever import PineconeRetriever

    path = REPO / "data" / "eval" / "retrieval.jsonl"
    if not path.exists():
        raise SystemExit(f"golden set 不存在, 先跑: python scripts/gen_golden_set.py")
    cases = [json.loads(line) for line in
             path.open(encoding="utf-8").read().splitlines() if line.strip()]
    if not cases:
        raise SystemExit("golden set 为空, 重新生成")

    retriever = PineconeRetriever()
    eval_cases: list[dict] = []
    for c in cases:
        results = await retriever.search(query=c["question"], top_k=10,
                                         rerank_top_n=10,
                                         namespace=None)
        # ranked id 与 golden set 向量 id 同源(Pinecone vector id);
        # ground truth = 该案例全部 chunk 的向量 id(gen 脚本 relevant_ids)
        ranked = [r.get("id") or r.get("case_number") or ""
                  for r in results]
        eval_cases.append({"ranked_ids": ranked,
                           "relevant_ids": c.get("relevant_ids")
                           or [c["case_id"]]})

    metrics = evaluate(eval_cases, k=5)
    await db.insert_eval_run("retrieval", label, metrics,
                             [{"case_id": c["case_id"]} for c in cases])
    print(json.dumps(metrics, ensure_ascii=False, indent=2))

    if baseline:
        pool = await db.get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT metrics FROM eval_runs WHERE label=%s "
                "AND dataset='retrieval' ORDER BY created_at DESC LIMIT 1",
                (baseline,))
            row = await cur.fetchone()
        await db.close_pool()
        if row:
            old = row[0] or {}
            print(f"\n-- baseline diff ({baseline} → {label}) --")
            for k, v in metrics.items():
                ov = float(old.get(k, 0) or 0)
                print(f"{k}: {ov:.4f} → {v:.4f} ({v - ov:+.4f})")
        else:
            print(f"baseline {baseline} 无记录, 跳过 diff")


async def run_calibration(label: str) -> None:
    """标定 suite: 全 golden set 跑新管线, 正/负 sigmoid 分布 → 建议阈值.

    输出分布分位数与 suggest_thresholds 建议值; 不落库(标定是离线动作)。

    Args:
        label: 本次标定的可追溯标签(记录进 spec §9)。
    """
    from lawApp_LangGraph.eval_metrics import split_scores, suggest_thresholds
    from lawApp_LangGraph.RAG_service.pinecone_retriever import PineconeRetriever

    path = REPO / "data" / "eval" / "retrieval.jsonl"
    if not path.exists():
        raise SystemExit(f"golden set 不存在, 先跑: python scripts/gen_golden_set.py")
    cases = [json.loads(l) for l in
             path.open(encoding="utf-8").read().splitlines() if l.strip()]
    if not cases:
        raise SystemExit("golden set 为空, 先跑: python scripts/gen_golden_set.py")

    retriever = PineconeRetriever()
    all_pos, all_neg = [], []
    rerank_missing = 0
    for c in cases:
        results = await retriever.search(query=c["question"], top_k=10,
                                          rerank_top_n=10, namespace=None)
        if results and "relevance_score" not in results[0]:
            rerank_missing += 1
            continue
        pos, neg = split_scores(results, c.get("relevant_ids") or [])
        all_pos.extend(pos)
        all_neg.extend(neg)

    print(f"label={label} | cases={len(cases)} | rerank_missing={rerank_missing}")
    print(f"pos={len(all_pos)} | neg={len(all_neg)}")
    if all_pos:
        sp = sorted(all_pos)
        print("pos 分位数 p10/p50/p90:",
              [round(sp[int(len(sp) * q)], 4) for q in (0.1, 0.5, 0.9)])
    if all_neg:
        sn = sorted(all_neg)
        print("neg 分位数 p10/p50/p90:",
              [round(sn[int(len(sn) * q)], 4) for q in (0.1, 0.5, 0.9)])
    print("建议阈值:", json.dumps(suggest_thresholds(all_pos, all_neg),
                                 ensure_ascii=False))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", choices=["retrieval", "calibration"],
                    default="retrieval")
    ap.add_argument("--label", required=True)
    ap.add_argument("--baseline", default=None)
    args = ap.parse_args()
    if args.suite == "calibration":
        await run_calibration(args.label)
    else:
        await run_retrieval(args.label, args.baseline)


if __name__ == "__main__":
    asyncio.run(main())
