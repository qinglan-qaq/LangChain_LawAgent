"""程序化执行 notebook 验证（子项目A ipynb 测试约定）。

用法: python scripts/run_nb.py notebooks/<nb.ipynb>
通过标准: 所有 cell 无异常执行完毕（末尾 cell 打印 ALL PASSED 由 notebook 自身保证）。
kernel_name 取 lawagent（已注册）; 若未注册则回退 python3。
"""
import subprocess
import sys
from pathlib import Path

import nbformat
from nbclient import NotebookClient

ROOT = Path(__file__).resolve().parent.parent


def _kernel_available(name: str) -> bool:
    """检查指定名称的 jupyter kernel 是否已注册。

    Args:
        name: kernel 名称，如 lawagent。

    Returns:
        已注册返回 True，否则返回 False。
    """
    out = subprocess.run(
        [sys.executable, "-m", "jupyter", "kernelspec", "list"],
        capture_output=True, text=True,
    )
    return name in out.stdout


def main() -> int:
    """程序化执行单个 notebook 的全部 cell，作为验证入口。

    Returns:
        退出码：所有 cell 无异常执行完毕返回 0，执行异常时抛出错误终止。
    """
    nb_path = Path(sys.argv[1]).resolve()
    nb = nbformat.read(nb_path, as_version=4)
    kernel = nb.metadata.get("kernelspec", {}).get("name", "lawagent")
    if kernel != "python3" and not _kernel_available(kernel):
        kernel = "python3"
    client = NotebookClient(
        nb, timeout=600, kernel_name=kernel,
        resources={"metadata": {"path": str(nb_path.parent)}},
    )
    client.execute()
    print(f"OK: {nb_path.name} all cells executed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
