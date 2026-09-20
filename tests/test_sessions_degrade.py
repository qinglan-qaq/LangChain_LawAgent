"""P0 — /sessions PG 断连降级(规格故事 22)。"""
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[str] = []

    def emit(self, record):
        self.records.append(record.getMessage())


def test_sessions_returns_empty_and_logs_when_pg_down(monkeypatch):
    import lawApp_LangGraph.db as db
    from lawApp_LangGraph.FastAPI.api import app
    from fastapi.testclient import TestClient

    async def _boom():
        raise RuntimeError("PG 掉线(模拟)")

    monkeypatch.setattr(db, "get_pool", _boom)

    cap = _Capture()
    agent_flow_logger = logging.getLogger("agent_flow")

    with TestClient(app) as client:
        # lifespan 的 setup_logging 会清理重挂 handler, 捕获器必须在启动完成后挂
        agent_flow_logger.addHandler(cap)
        try:
            r = client.get("/sessions")
        finally:
            agent_flow_logger.removeHandler(cap)

    assert r.status_code == 200
    assert r.json() == []
    # 降级必须有日志信号(不被静默吞掉)
    assert any("会话列表降级" in m for m in cap.records), "降级必须留下 ERROR 日志痕迹"
