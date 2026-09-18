"""
MCP 客户端挂载 — agent 自举侧 (A2)

职责:
    连接自建 law-search MCP server (http://127.0.0.1:9381/mcp,
    经 MCP_SERVER_URL 可配), 把远端 search_laws / search_cases /
    recall_memory 拉成本地 BaseTool, 注入 agent 的 ALL_TOOLS。

设计取舍:
    - 连接失败时优雅降级: 本地工具(fetch_laws / retrieve_legal_knowledge /
    search_memory)与 MCP 工具功能重合, server 不在线不影响图可用
    - 会话持有: MultiServerMCPClient 是 async context manager,
    连接由 mcp_client 全局持有; 重复调用 get_mcp_tools() 幂等
    - 工具名去重: MCP 工具与本地工具语义重合 → 只在本地无同名工具时注册,
    避免 planner/ToolNode 出现重名歧义; 通过 MCP_TOOLS_ENABLED=0 关闭

接入点: runtime.setup_runtime() 在装配图之前调用 get_mcp_tools()
"""

from __future__ import annotations

import logging
from typing import List

from lawApp_LangGraph.config import settings

logger = logging.getLogger("lawApp.mcp_client")

# 已拉取的 MCP 工具(进程级缓存)
_mcp_tools: List = []
_client = None  # MultiServerMCPClient 实例, 持有会话
_loaded = False


async def get_mcp_tools() -> List:
    """连接 MCP server 并返回远端工具列表(失败/关闭时返回空列表)。

    幂等: 首次调用建立连接, 之后直接返回缓存。
    """
    global _mcp_tools, _client, _loaded
    if _loaded:
        return _mcp_tools
    _loaded = True

    if settings.mcp_tools_enabled.lower() in ("0", "false", "no"):
        logger.info("MCP 工具挂载已通过 MCP_TOOLS_ENABLED 关闭")
        return _mcp_tools

    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient

        _client = MultiServerMCPClient(
            {
                "law-search": {
                    "url": settings.mcp_server_url,
                    "transport": "streamable_http",  # adapters TypedDict 字面量(非 fastmcp 的 'streamable-http')
                }
            }
        )
        _mcp_tools = await _client.get_tools()
        logger.info(
            "MCP 工具挂载成功 | server=%s | tools=%s",
            settings.mcp_server_url,
            [t.name for t in _mcp_tools],
        )
    except Exception as e:
        # server 不在线 → 降级: 本地工具语义重合, 图不受影响
        logger.warning("MCP server 不可用, 跳过挂载: %s", str(e)[:150])
        _mcp_tools = []
    return _mcp_tools


async def close_mcp() -> None:
    """断开 MCP 会话(应用关闭时调用)。"""
    global _mcp_tools, _client, _loaded
    if _client is not None:
        try:
            await _client.__aexit__(None, None, None)
        except Exception as e:  # pragma: no cover
            logger.warning("MCP 会话关闭异常: %s", e)
    _client = None
    _mcp_tools = []
    _loaded = False
