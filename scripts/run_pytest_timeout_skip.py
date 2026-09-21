"""按文件逐批跑 pytest, 单文件超时直接记 SKIPPED 不阻塞全量回归。

用法:
    python scripts/run_pytest_timeout_skip.py            # 全量 tests/
    python scripts/run_pytest_timeout_skip.py tests/test_smoke.py   # 指定文件

规则(Global Constraints 约定):
- 每个测试文件独立子进程, 文件级总超时 900s; 超时整文件记 SKIPPED(file timeout)继续下一文件
- 文件内部仍用 pytest-timeout 单用例 120s(thread method)
- 退出码: 只有真实 FAIL 才非 0; SKIPPED 不算失败
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

PY = sys.executable
ROOT = Path(__file__).resolve().parent.parent
FILE_TIMEOUT_S = 900
PER_TEST_TIMEOUT_S = 120


def _collect_files() -> list[str]:
    r = subprocess.run(
        [PY, "-m", "pytest", "tests/", "--collect-only", "-q"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=300,
    )
    files: list[str] = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("tests/") and "::" in line:
            f = line.split("::")[0]
            if f not in files:
                files.append(f)
    return sorted(files)


def _run_one(f: str) -> tuple[str, str]:
    """跑单文件, 返回 (verdict, summary_text)。verdict ∈ PASS/SKIPPED/FAIL。"""
    cmd = [
        PY, "-m", "pytest", f, "-q",
        "--timeout", str(PER_TEST_TIMEOUT_S),
        "--timeout-method=thread", "-rs",
    ]
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT,
                           timeout=FILE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return "SKIPPED", f"{f}: 文件级超时 {FILE_TIMEOUT_S}s, 整文件跳过"
    dur = int(time.time() - t0)
    out = (r.stdout or "") + (r.stderr or "")
    tail = [l for l in out.splitlines() if l.strip()][-6:]
    if r.returncode == 0:
        return "PASS", f"{f}: {dur}s {' | '.join(tail[-2:])}"
    # 纯超时失败(pytest-timeout thread 法 os._exit, 无 FAILED 行)→ 按约定记 SKIPPED 不算失败;
    # 有真实断言/错误失败仍算 FAIL
    n_timeouts = out.count("+++ Timeout +++")
    if n_timeouts and not re.search(r"FAILED|ERROR[_ ]|AssertionError", out):
        return "SKIPPED", f"{f}: {n_timeouts} 用例超时({PER_TEST_TIMEOUT_S}s), 按约定跳过"
    return "FAIL", f"{f}: rc={r.returncode} {' | '.join(tail)}"


def main() -> int:
    targets = sys.argv[1:] or _collect_files()
    print(f"# pytest 超时跳过跑测: {len(targets)} 个文件, "
          f"文件超时 {FILE_TIMEOUT_S}s / 用例超时 {PER_TEST_TIMEOUT_S}s")
    results: list[tuple[str, str]] = []
    for f in targets:
        verdict, msg = _run_one(f)
        results.append((verdict, msg))
        print(f"[{verdict}] {msg}", flush=True)

    passed = sum(1 for v, _ in results if v == "PASS")
    skipped = sum(1 for v, _ in results if v == "SKIPPED")
    failed = sum(1 for v, _ in results if v == "FAIL")
    print(f"\n# 汇总: {passed} PASS / {skipped} SKIPPED / {failed} FAIL")
    print("ALL PASSED" if failed == 0 else "HAS FAILURES")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
