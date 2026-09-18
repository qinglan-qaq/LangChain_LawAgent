"""批量执行 tests_ipynb/ 下全部体检册, 汇总各册结论。

用法:
    python tests_ipynb/run_all.py            # 跑全部
    python tests_ipynb/run_all.py 01 05      # 只跑文件名含 01 / 05 的册
    python tests_ipynb/run_all.py --save     # 执行结果写回 .ipynb(可用 Jupyter 打开看输出)

退出码: 任一册结论为 HAS FAILURES 则返回 1, 便于接入 CI。
"""

import sys
from pathlib import Path

import nbformat
from nbclient import NotebookClient

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
VERDICTS = ("ALL PASSED", "HAS FAILURES")


def _cell_text(nb) -> str:
    """把 notebook 所有 cell 的 stdout/stderr 文本拼成一份。

    Args:
        nb: 已执行的 notebook 对象。

    Returns:
        str: 全部流式输出拼接结果(错误输出额外带 ERROR 前缀)。
    """
    parts: list[str] = []
    for cell in nb.cells:
        for out in cell.get("outputs", []):
            if out.get("output_type") == "stream":
                parts.append(out.get("text", ""))
            elif out.get("output_type") == "error":
                parts.append(f"ERROR {out.get('ename')}: {out.get('evalue', '')[:300]}\n")
    return "".join(parts)


def _summary_block(text: str) -> str:
    """截取报告末尾的汇总段(自含「汇总」字样的行起)。

    Args:
        text: cell 输出全文。

    Returns:
        str: 汇总段; 没有汇总段时返回最后 40 行。
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "汇总" in line:
            return "\n".join(lines[i:])
    return "\n".join(lines[-40:])


def _verdict(text: str) -> str:
    """从输出里提取结论行。

    Args:
        text: cell 输出全文。

    Returns:
        str: ALL PASSED / HAS FAILURES / UNKNOWN。
    """
    for v in VERDICTS:
        if v in text:
            return v
    return "UNKNOWN"


def main() -> int:
    """执行目标体检册并打印汇总表。

    Returns:
        int: 退出码, 存在 FAIL 返回 1。
    """
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    save = "--save" in sys.argv

    files = sorted(p for p in HERE.glob("*.ipynb") if not p.name.startswith("_"))
    if args:
        files = [p for p in files if any(a in p.name for a in args)]
    if not files:
        print("没有匹配的体检册")
        return 1

    results: list[tuple[str, str, dict]] = []
    for path in files:
        nb = nbformat.read(path, as_version=4)
        kernel = nb.metadata.get("kernelspec", {}).get("name", "lawapp")
        print(f"\n{'=' * 70}\n执行 {path.name} (kernel={kernel})\n{'=' * 70}")
        try:
            NotebookClient(
                nb,
                timeout=1800,
                kernel_name=kernel,
                resources={"metadata": {"path": str(HERE)}},
            ).execute()
        except Exception as e:
            print(f"执行异常: {type(e).__name__}: {str(e)[:400]}")
            results.append((path.name, "ERROR", {"PASS": 0, "FAIL": 0, "SKIP": 0}))
            continue

        text = _cell_text(nb)
        verdict = _verdict(text)
        print(text if verdict == "HAS FAILURES" else _summary_block(text))
        counts = {
            k: sum(1 for line in text.splitlines() if line.startswith(f"[{k}]"))
            for k in ("PASS", "FAIL", "SKIP")
        }
        results.append((path.name, verdict, counts))
        if save:
            nbformat.write(nb, path)

    print(f"\n{'=' * 70}\n总览\n{'=' * 70}")
    width = max(len(name) for name, _, _ in results)
    for name, verdict, c in results:
        print(
            f"{name:<{width}}  {verdict:<13} PASS {c['PASS']:>2} / FAIL {c['FAIL']:>2} / SKIP {c['SKIP']:>2}"
        )
    bad = [n for n, v, _ in results if v != "ALL PASSED"]
    print()
    if bad:
        print("未全绿: " + ", ".join(bad))
        return 1
    print("全部体检册 ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
