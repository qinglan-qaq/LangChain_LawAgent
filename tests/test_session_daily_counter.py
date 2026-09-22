"""new_session_id 按天计数(零点重置 + PG 重启恢复基数)回归测试。

不依赖真 PG —— _seed_daily_seq 全部 monkeypatch(参照
tests/test_hitl_single_question.py 的替身模式)。覆盖:
    1. 同日连续发号: 日级序号递增(001→002→003), 格式不变
    2. 日键跨天(零点): 编号重置为 001(不沿用旧日计数)
    3. AT / AS 前缀各自独立计数
    4. PG seed 基数生效(7 → 下一号 008); seed 异常/0 时
       降级为进程内计数, 发号不受影响
"""
import re
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_SID_RE = re.compile(r"^(AT|AS)-\d{8}-\d{6}-\d{3,}$")


@pytest.fixture(autouse=True)
def _reset_sid_state():
    """每个用例前后清空计数器/seed 缓存, 测试间互不污染。"""
    import lawApp_LangGraph.FastAPI.utils as fastapi_utils

    fastapi_utils._SID_COUNTER.clear()
    fastapi_utils._SID_SEEDED.clear()
    yield
    fastapi_utils._SID_COUNTER.clear()
    fastapi_utils._SID_SEEDED.clear()


def test_daily_counter_same_day_increment(monkeypatch):
    """同日连续 3 次发号: 末段序号递增 001→002→003, 格式不变。"""
    import lawApp_LangGraph.FastAPI.utils as fastapi_utils

    # 离线可跑: seed 固定 0, 不触真实 PG
    monkeypatch.setattr(fastapi_utils, "_seed_daily_seq",
                       lambda prefix, day_key: 0)
    sids = [fastapi_utils.new_session_id("attorney") for _ in range(3)]
    for sid in sids:
        assert _SID_RE.match(sid), f"格式必须为 模式-时间-编号: {sid}"
    seqs = [int(sid.rsplit("-", 1)[1]) for sid in sids]
    assert seqs == [1, 2, 3], "同日序号必须从 001 起连续递增"
    # 日键段(YYYYMMDD)均为今天
    today = f"{datetime.now():%Y%m%d}"
    assert all(sid.split("-")[1] == today for sid in sids)


def test_daily_counter_resets_on_new_day(monkeypatch):
    """日键从旧日期(20200101)跨到今天 → 编号重置为 001, 不沿用旧日计数 005。"""
    import lawApp_LangGraph.FastAPI.utils as fastapi_utils

    monkeypatch.setattr(fastapi_utils, "_seed_daily_seq",
                       lambda prefix, day_key: 0)
    fastapi_utils._SID_COUNTER["attorney"] = ("20200101", 5)

    sid = fastapi_utils.new_session_id("attorney")
    assert _SID_RE.match(sid)
    today = f"{datetime.now():%Y%m%d}"
    parts = sid.split("-")
    assert parts[1] == today, "日键必须翻到今天"
    assert int(parts[3]) == 1, "跨天后编号必须重置为 001(不是 006)"


def test_prefixes_independent(monkeypatch):
    """AT 与 AS 交替发号: 各前缀序号独立递增, 互不串号。"""
    import lawApp_LangGraph.FastAPI.utils as fastapi_utils

    # 离线可跑: seed 固定 0, 不触真实 PG
    monkeypatch.setattr(fastapi_utils, "_seed_daily_seq",
                       lambda prefix, day_key: 0)
    nsid = fastapi_utils.new_session_id
    at1 = nsid("attorney")
    as1 = nsid("assistant")
    at2 = nsid("attorney")
    as2 = nsid("assistant")
    at3 = nsid("attorney")

    assert at1.startswith("AT-") and at2.startswith("AT-") and at3.startswith("AT-")
    assert as1.startswith("AS-") and as2.startswith("AS-")
    assert int(at1.rsplit("-", 1)[1]) == 1
    assert int(at2.rsplit("-", 1)[1]) == 2
    assert int(at3.rsplit("-", 1)[1]) == 3, "AS 发号不得影响 AT 计数"
    assert int(as1.rsplit("-", 1)[1]) == 1
    assert int(as2.rsplit("-", 1)[1]) == 2, "AT 发号不得影响 AS 计数"


def test_seed_degraded_on_pg_error(monkeypatch):
    """seed 基数生效(7 → 008); seed 抛异常/返回 0 时进程内计数正常发号。"""
    import lawApp_LangGraph.FastAPI.utils as fastapi_utils

    # a) PG 恢复基数: 当天已有 7 个 AT 会话 → 下一号 008
    monkeypatch.setattr(fastapi_utils, "_seed_daily_seq",
                       lambda prefix, day_key: 7)
    fastapi_utils._SID_SEEDED.clear()
    sid = fastapi_utils.new_session_id("attorney")
    assert _SID_RE.match(sid)
    assert sid.rsplit("-", 1)[1] == "008", "seed=7 时下一号必须为 008"

    # b) seed 抛异常(如 PG 掉线且实现未内部吞) → 降级进程内计数, 001 起发号
    def _boom(prefix, day_key):
        raise RuntimeError("PG down")

    monkeypatch.setattr(fastapi_utils, "_seed_daily_seq", _boom)
    fastapi_utils._SID_COUNTER.clear()
    fastapi_utils._SID_SEEDED.clear()
    sid = fastapi_utils.new_session_id("attorney")
    assert _SID_RE.match(sid)
    assert sid.rsplit("-", 1)[1] == "001", "seed 异常时必须降级为进程内计数从 001 发号"

    # c) seed 返回 0(PG 无当日会话/掉线吞异常) → 进程内计数正常
    monkeypatch.setattr(fastapi_utils, "_seed_daily_seq",
                       lambda prefix, day_key: 0)
    sid2 = fastapi_utils.new_session_id("attorney")
    assert sid2.rsplit("-", 1)[1] == "002", "同日后续发号在进程内计数上递增"
