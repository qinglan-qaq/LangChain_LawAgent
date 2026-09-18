"""Windows 事件循环工厂 — psycopg_async 在 win32 上必须用 SelectorEventLoop。

uvicorn 启动命令需追加(见 README):
    --loop lawApp_LangGraph.FastAPI.loop:selector_loop_factory
"""

from __future__ import annotations

import asyncio
import sys


def selector_loop_factory() -> asyncio.AbstractEventLoop:
    """uvicorn --loop 自定义工厂: win32 返回 SelectorEventLoop, 其余走默认。"""
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()
