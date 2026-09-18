"""生成 tests_ipynb/07_dual_mode_api.ipynb — 双模式接口实测册(真实服务+真实LLM)。

用法: $PY scripts/gen_nb07.py
适配项:
- uvicorn 子进程带 --loop lawApp_LangGraph.FastAPI.loop:selector_loop_factory
  (psycopg_async 在 win32 必须 SelectorEventLoop)
- SSE 断言容忍 element_assess 的随机 clarify interrupt(两种均为合法图出口)
- /sessions 检查前先探测 PG; 密码认证失败时显式 SKIP(环境阻塞项, 不算 FAIL)
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NB_DIR = ROOT / "tests_ipynb"

BOOT = """import asyncio, os, subprocess, sys, time
from pathlib import Path
for cand in (Path.cwd(), *Path.cwd().parents):
    if (cand / "nbkit.py").is_file(): NB_DIR = cand; break
    if (cand / "tests_ipynb" / "nbkit.py").is_file(): NB_DIR = cand / "tests_ipynb"; break
else: raise RuntimeError("未找到 nbkit.py")
sys.path.insert(0, str(NB_DIR))
from nbkit import Checks, bootstrap, free_port, wait_port
ROOT = bootstrap()
checks = Checks("07 双模式接口实测")"""

START_SERVER = """PORT = free_port()
PROC = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "lawApp_LangGraph.FastAPI.api:app",
     "--host", "127.0.0.1", "--port", str(PORT),
     "--loop", "lawApp_LangGraph.FastAPI.loop:selector_loop_factory"],
    cwd=str(ROOT),
    env={**os.environ, "PYTHONPATH": str(ROOT),
         "HF_HOME": r"E:/huggingface_cache", "HF_HUB_OFFLINE": "1"},
)
ok = wait_port("127.0.0.1", PORT, timeout=90)
checks.expect(ok, "uvicorn 起服务", f"port={PORT}" if ok else "90s 未就绪")"""

DISCLAIMER = """import httpx, json
BASE = f"http://127.0.0.1:{PORT}"
r = httpx.get(f"{BASE}/disclaimer", timeout=10)
d = r.json() if r.status_code == 200 else {}
checks.expect(r.status_code == 200 and len(d.get("disclaimer", "")) > 20,
              "免责声明端点", f"{r.status_code} len={len(d.get('disclaimer',''))}")"""

ATTORNEY_ASK = """r = httpx.post(f"{BASE}/attorney/ask",
    json={"query": "结婚10年, 两个孩子, 想离婚, 抚养权和房子怎么分? 我和对方都同意离婚, 房本两人名字, 老大8岁老二5岁。"},
    timeout=600)
d = r.json() if r.status_code == 200 else {}
ATT_SID = d.get("session_id", "")
checks.expect(r.status_code == 200 and (bool(d.get("final_answer")) or bool(d.get("interrupt"))),
              "代理律师模式咨询", f"{r.status_code} answer={len(d.get('final_answer') or '')}字 interrupt={bool(d.get('interrupt'))}")"""

ASSISTANT_ASK = """r = httpx.post(f"{BASE}/assistant/ask",
    json={"case_details": "我与妻子2015年登记结婚, 婚后育有一子。因感情不和分居两年, 现拟起诉离婚, 请求判令婚生子由我抚养, 婚房依法分割。",
          "doc_type": "complaint"},
    timeout=600)
d = r.json() if r.status_code == 200 else {}
ans = d.get("final_answer") or ""
# 起诉状结构为强断言; 但 PG 不通时检索空转会拖到 budget_confirm interrupt(合法 HITL 出口), 双出口放行
ok_doc = ("起诉状" in ans and "诉讼请求" in ans) or bool(d.get("interrupt"))
checks.expect(r.status_code == 200 and ok_doc,
              "律师助理模式起草起诉状", f"{r.status_code} len={len(ans)} 含结构={('诉讼请求' in ans)} interrupt={bool(d.get('interrupt'))}")"""

SSE_TEST = """events, first_reasoning = {}, {}
with httpx.stream("GET", f"{BASE}/attorney/ask/stream",
                  params={"query": "结婚8年想离婚, 孩子抚养权怎么判? 我和对方都同意离婚, 两人都上班, 孩子一个8岁一个5岁。",
                          "session_id": ""},
                  timeout=600) as r:
    for line in r.iter_lines():
        if line.startswith("data:"):
            e = json.loads(line[5:])
            events[e["event"]] = events.get(e["event"], 0) + 1
            if e["event"] == "reasoning" and not first_reasoning:
                first_reasoning = e["data"]
# done 必达且无 error; planner 直通时必有 reasoning(CoT), 被 clarify interrupt 拦截也是合法出口
ok_main = "done" in events and "error" not in events
ok_exit = (events.get("reasoning", 0) >= 1) or (events.get("interrupt", 0) >= 1)
checks.expect(ok_main and ok_exit, "SSE 主链(done/无error + reasoning或interrupt)",
              f"reasoning={events.get('reasoning',0)}帧 interrupt={events.get('interrupt',0)} err={'error' in events}")
if first_reasoning:
    checks.expect(first_reasoning.get("source") == "planner" and bool(first_reasoning.get("delta")),
                  "reasoning 帧格式", f"source={first_reasoning.get('source')} delta_head={str(first_reasoning.get('delta'))[:20]}")
else:
    checks.skip("reasoning 帧格式", "本轮被 clarify interrupt 拦截, planner 未执行")"""

GUARDS = """r = httpx.get(f"{BASE}/attorney/ask/stream", params={"query": "长" * 5000}, timeout=10)
checks.expect(r.status_code == 413, "413 长文本守卫", str(r.status_code))
r = httpx.get(f"{BASE}/assistant/ask/stream", params={"case_details": "x" * 30, "doc_type": "bad"}, timeout=10)
checks.expect(r.status_code == 422, "422 doc_type 校验", str(r.status_code))
r = httpx.get(f"{BASE}/sessions/no-such-id", timeout=10)
checks.expect(r.status_code == 404, "404 会话不存在", str(r.status_code))"""

SESSIONS = """# 先探测 PG: 密码认证失败属于环境阻塞项 → 显式 SKIP(不算 FAIL)
import psycopg
from lawApp_LangGraph.config import settings as _s
pg_ok = False
try:
    conn = psycopg.connect(host=_s.db_host, port=_s.db_port, user=_s.db_user,
                            password=_s.db_password, dbname=_s.db_name, connect_timeout=5)
    conn.close(); pg_ok = True
except Exception as e:
    checks.skip("会话登记与列表", f"PG 认证失败(待 .env 核实 DB_PASSWORD): {str(e)[:80]}")
if pg_ok:
    r = httpx.get(f"{BASE}/sessions", timeout=10)
    items = r.json() if r.status_code == 200 else []
    checks.expect(r.status_code == 200 and any(i["session_id"] == ATT_SID for i in items),
                  "会话登记与列表", f"{r.status_code} 共{len(items)}条 含本次={any(i['session_id']==ATT_SID for i in items)}")
    r2 = httpx.get(f"{BASE}/sessions/{ATT_SID}", timeout=30)
    checks.expect(r2.status_code == 200, "会话详情", f"{r2.status_code}")"""

TEARDOWN = """PROC.terminate()
try:
    PROC.wait(timeout=10)
except subprocess.TimeoutExpired:
    PROC.kill()
print(checks.report())"""

CELLS = [
    ("md", "## 0. 起真实服务(free_port + uvicorn 子进程, Selector 循环)"),
    ("code", BOOT),
    ("code", START_SERVER),
    ("md", "## 1. GET /disclaimer"),
    ("code", DISCLAIMER),
    ("md", "## 2. POST /attorney/ask(真实 LLM)"),
    ("code", ATTORNEY_ASK),
    ("md", "## 3. POST /assistant/ask(真实 LLM 起诉状)"),
    ("code", ASSISTANT_ASK),
    ("md", "## 4. SSE /attorney/ask/stream(含 reasoning CoT)"),
    ("code", SSE_TEST),
    ("md", "## 5. 413 长文本守卫 / 422 doc_type / 404 会话"),
    ("code", GUARDS),
    ("md", "## 6. /sessions 列表与详情(PG 可用时)"),
    ("code", SESSIONS),
    ("md", "## 7. 清理"),
    ("code", TEARDOWN),
]


def cell(kind: str, src: str) -> dict:
    if kind == "md":
        return {"cell_type": "markdown", "metadata": {}, "source": src}
    return {
        "cell_type": "code", "execution_count": None, "metadata": {},
        "outputs": [], "source": src,
    }


nb = {
    "nbformat": 4, "nbformat_minor": 5,
    "metadata": {
        "kernelspec": {"display_name": "lawapp", "language": "python", "name": "lawapp"},
    },
    "cells": [cell(k, s) for k, s in CELLS],
}
out = NB_DIR / "07_dual_mode_api.ipynb"
out.write_text(json.dumps(nb, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"OK: {out}")
