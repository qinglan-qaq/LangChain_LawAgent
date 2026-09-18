"""MCP 子包 — law-search server 与 agent 自举侧客户端。

刻意不在本 __init__ 中 re-export 子模块符号:
    mcp_server 在导入期即构造 FastMCP 实例、mcp_client 在导入期读 settings,
    二者均有副作用。`import lawApp_LangGraph.mcp` 应当保持无副作用,
    使用方请显式导入子模块(如 `from lawApp_LangGraph.mcp.mcp_server import mcp`)。

注意: 本包名 mcp 与第三方 SDK(site-packages 的 mcp)同名。Python 3 为绝对导入,
子模块内 `from mcp.server.fastmcp import FastMCP` 仍解析到第三方 SDK,
不会被本包遮蔽。
"""
