"""ipynb 配置体检共用工具 — tests_ipynb/ 各体检册共享。

设计要点:
    - 不依赖 pytest。配置体检的 FAIL 是结论而非崩溃, 故用 PASS/FAIL/SKIP
      三态收集, 每册末尾统一出表, 便于回答「现在这套配置到底能不能用」。
    - .env 按文本直接解析, 与 pydantic-settings 的读取结果分开报告,
      用以暴露「文件里写了值但代码没读到」这类隐性偏差。
    - 所有网络/子进程探针自带超时。历史教训: stdio MCP 子进程握手曾因
      无超时而长时间挂起(见 commit 1c71708 与 tests/test_mcp.py 的注释)。

用法(notebook 首格):
    import sys
    from pathlib import Path

    for cand in (Path.cwd(), *Path.cwd().parents):
        if (cand / "nbkit.py").is_file():
            NB_DIR = cand
            break
        if (cand / "tests_ipynb" / "nbkit.py").is_file():
            NB_DIR = cand / "tests_ipynb"
            break
    sys.path.insert(0, str(NB_DIR))

    from nbkit import Checks, bootstrap
    ROOT = bootstrap()
    checks = Checks("01 环境配置体检")
"""

from __future__ import annotations

import os
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"


def bootstrap() -> Path:
    """把仓库根与 tests_ipynb/ 注入 sys.path, 返回仓库根。

    支持两种执行方式: cwd 为仓库根(run_all.py / 手动)或为 tests_ipynb/
    (nbclient 设 resources path 为 notebook 所在目录)。

    Returns:
        Path: 仓库根目录(含 lawApp_LangGraph/ 的那一层)。

    Raises:
        RuntimeError: 向上找不到含 tests_ipynb/ 的目录。
    """
    for cand in (Path.cwd(), *Path.cwd().parents):
        if (cand / "lawApp_LangGraph").is_dir() and (cand / "tests_ipynb").is_dir():
            root = cand
            break
    else:
        raise RuntimeError("未找到仓库根(需同时含 lawApp_LangGraph/ 与 tests_ipynb/)")
    for p in (root, root / "tests_ipynb"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    return root


def env_path(root: Path) -> Path:
    """返回 .env 的约定路径(包内, 非仓库根)。

    Args:
        root: 仓库根目录。

    Returns:
        Path: <root>/lawApp_LangGraph/.env
    """
    return root / "lawApp_LangGraph" / ".env"


def parse_env(root: Path) -> dict[str, str]:
    """按文本解析 .env(不展开变量、不注入 os.environ)。

    Args:
        root: 仓库根目录。

    Returns:
        dict[str, str]: 文件里出现的键值对; 文件缺失时为空 dict。
        行内注释(空格 + #)按 python-dotenv 的习惯截断。
    """
    path = env_path(root)
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.split(" #")[0].strip()
    return out


def mask(value: str, head: int = 4, tail: int = 2) -> str:
    """把凭据类取值脱敏后用于报告。

    Args:
        value: 原始取值。
        head: 保留的开头字符数。
        tail: 保留的结尾字符数。

    Returns:
        str: 形如 ``sk-1…9f (len=35)``; 空值返回 ``(空)``, 短值只留长度。
    """
    v = value or ""
    if not v:
        return "(空)"
    if len(v) <= head + tail:
        return f"*** (len={len(v)})"
    return f"{v[:head]}…{v[-tail:]} (len={len(v)})"


def _w(text: str) -> int:
    """按终端显示宽度估算字符串宽度(CJK 记 2)。

    Args:
        text: 待估算文本。

    Returns:
        int: 估算宽度, 仅用于表格对齐。
    """
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in str(text))


def _pad(text: str, width: int) -> str:
    """按显示宽度右补空格。

    Args:
        text: 待补文本。
        width: 目标显示宽度。

    Returns:
        str: 补齐后的文本(超宽原样返回)。
    """
    return str(text) + " " * max(0, width - _w(text))


def hf_hub_dir() -> Path:
    """返回 HuggingFace Hub 缓存目录。

    优先 HF_HOME(与进程 env 一致), 其次默认 ~/.cache/huggingface。

    Returns:
        Path: 缓存目录下的 hub/ 路径。
    """
    home = os.getenv("HF_HOME")
    base = Path(home) if home else Path.home() / ".cache" / "huggingface"
    return base / "hub"


def hf_model_cached(repo_id: str) -> bool:
    """判断模型快照是否已在本机缓存中。

    Args:
        repo_id: 形如 ``BAAI/bge-large-zh-v1.5`` 的模型仓库名。

    Returns:
        bool: 缓存目录中存在对应 models--<org>--<name> 即为 True。
    """
    return (hf_hub_dir() / f"models--{repo_id.replace('/', '--')}").is_dir()


def tcp_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """探测目标端口是否可连接。

    Args:
        host: 主机名或 IP。
        port: 端口号。
        timeout: 连接超时秒数。

    Returns:
        bool: 建连成功为 True, 其余为 False。
    """
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def wait_port(host: str, port: int, timeout: float = 20.0) -> bool:
    """轮询等待端口就绪(用于子进程起的服务)。

    Args:
        host: 主机名或 IP。
        port: 端口号。
        timeout: 最长等待秒数。

    Returns:
        bool: 超时前就绪返回 True。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if tcp_open(host, port, timeout=0.5):
            return True
        time.sleep(0.3)
    return False


def free_port() -> int:
    """挑一个当前空闲的本地端口。

    注意: 存在释放到再次绑定之间的竞态, 仅用于测试。

    Returns:
        int: 操作系统分配的端口号。
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class Checks:
    """PASS/FAIL/SKIP 三态体检结果收集器。

    Attributes:
        title: 体检册标题, 出现在汇总表头。
        rows: (名称, 状态, 说明) 三元组列表, 按登记顺序保存。
    """

    title: str = "体检"
    rows: list = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> str:
        """登记一条检查结果并即时打印。

        Args:
            name: 检查项名称。
            status: PASS / FAIL / SKIP。
            detail: 附注(实测值、耗时、修复提示等)。

        Returns:
            str: 登记的状态, 便于链式判断。
        """
        self.rows.append((name, status, detail))
        line = f"[{status}] {name}"
        print(f"{line} | {detail}" if detail else line)
        return status

    def ok(self, name: str, detail: str = "") -> str:
        """登记 PASS。"""
        return self.add(name, PASS, detail)

    def fail(self, name: str, detail: str = "") -> str:
        """登记 FAIL。"""
        return self.add(name, FAIL, detail)

    def skip(self, name: str, detail: str = "") -> str:
        """登记 SKIP(前置条件不满足, 非失败)。"""
        return self.add(name, SKIP, detail)

    def expect(
        self, cond: bool, name: str, ok_detail: str = "", fail_detail: str = ""
    ) -> str:
        """按条件登记 PASS 或 FAIL。

        Args:
            cond: 断言条件。
            name: 检查项名称。
            ok_detail: 通过时的说明。
            fail_detail: 失败时的说明(应给出可操作的修复提示)。

        Returns:
            str: PASS 或 FAIL。
        """
        return self.ok(name, ok_detail) if cond else self.fail(name, fail_detail)

    def counts(self) -> dict:
        """统计各状态条数。

        Returns:
            dict: ``{"PASS": n, "FAIL": n, "SKIP": n}``。
        """
        out = {PASS: 0, FAIL: 0, SKIP: 0}
        for _, status, _ in self.rows:
            out[status] = out.get(status, 0) + 1
        return out

    @property
    def has_fail(self) -> bool:
        """是否存在 FAIL。"""
        return any(status == FAIL for _, status, _ in self.rows)

    def report(self) -> str:
        """打印汇总表并返回结论行。

        无 FAIL 时返回 ``ALL PASSED``(与仓库既有 notebook 约定一致),
        否则返回 ``HAS FAILURES`` 并列出失败项。

        Returns:
            str: 结论行文本。
        """
        name_w = max([_w(r[0]) for r in self.rows] + [10])
        print()
        print(f"{self.title} — 汇总")
        print("=" * (name_w + 24))
        for name, status, detail in self.rows:
            mark = {PASS: "✓", FAIL: "✗", SKIP: "-"}[status]
            print(f"{mark} {_pad(name, name_w)} {_pad(status, 5)} {detail}")
        print("=" * (name_w + 24))
        c = self.counts()
        print(f"合计: PASS {c[PASS]} / FAIL {c[FAIL]} / SKIP {c[SKIP]}")
        if self.has_fail:
            print("\nHAS FAILURES:")
            for name, status, detail in self.rows:
                if status == FAIL:
                    print(f"  ✗ {name} | {detail}")
            return "HAS FAILURES"
        print("\nALL PASSED")
        return "ALL PASSED"
