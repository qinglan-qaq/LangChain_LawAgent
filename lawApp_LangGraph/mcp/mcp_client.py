"""
MCP 客户端挂载 — agent 自举侧 (A2)

职责:
    连接自建 law-search MCP server (http://127.0.0.1:9381/mcp,
    经 MCP_SERVER_URL 可配), 把远端 search_laws / search_cases /
    recall_memory 拉成本地 BaseTool, 注入 agent 的 ALL_TOOLS。

设计取舍:
    - 连接失败时优雅降级: 本地工具(fetch_laws / retrieve_legal_knowledge /
    search_memory)与 MCP 工具功能重合, server 不在线不影响图可用
    - 失败可重试(H8): _loaded 仅在成功拉取后置 True —— server 晚起时
    下次 get_mcp_tools() 再试, 不永久缓存失败状态
    - 挂死保护(H8): get_tools() 包 asyncio.wait_for(10s), server 挂 TCP
    时快速失败, 不拖死整个应用启动
    - 会话模型: langchain-mcp-adapters 0.3.x 的 MultiServerMCPClient 不是
    async context manager(__aenter__ 抛 NotImplementedError), 会话按
    get_tools/工具调用临时建立, 无需进程级持有或显式关闭
    - 工具名去重: MCP 工具与本地工具语义重合 → 只在本地无同名工具时注册,
    避免 planner/ToolNode 出现重名歧义; 通过 MCP_TOOLS_ENABLED=0 关闭

接入点: runtime.setup_runtime() 在装配图之前调用 get_mcp_tools()
"""

from __future__ import annotations

import asyncio
import logging
from typing import List

from lawApp_LangGraph.config import settings

logger = logging.getLogger("lawApp.mcp_client")

# 已拉取的 MCP 工具(进程级缓存, 仅成功拉取后有效)
_mcp_tools: List = []
_client = None  # MultiServerMCPClient 实例(0.3.x 无需持有会话, 仅留引用便于排查)
_loaded = False  # 语义: 上次挂载曾成功(失败保持 False, 下次调用重试)

# get_tools 挂死保护超时(秒): streamable_http 握手/工具枚举
_MCP_GET_TOOLS_TIMEOUT = 10.0


async def get_mcp_tools() -> List:
    """连接 MCP server 并返回远端工具列表(失败/关闭时返回空列表)。

    幂等: 首次调用建立连接, 成功后直接返回缓存;失败不缓存(H8 可重试)。
    """
    global _mcp_tools, _client, _loaded
    if _loaded:
        return _mcp_tools

    if settings.mcp_tools_enabled.lower() in ("0", "false", "no"):
        logger.info("MCP 工具挂载已通过 MCP_TOOLS_ENABLED 关闭")
        return _mcp_tools

    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient

        client = MultiServerMCPClient(
            {
                "law-search": {
                    "url": settings.mcp_server_url,
                    "transport": "streamable_http",  # adapters TypedDict 字面量(非 fastmcp 的 'streamable-http')
                }
            }
        )
        # H8: server 挂 TCP / 不回包时 get_tools 无限等 → 整个 app 启动挂死,
        # 包 wait_for 快速失败留待下次重试
        tools = await asyncio.wait_for(
            client.get_tools(), timeout=_MCP_GET_TOOLS_TIMEOUT
        )
        _client = client
        _mcp_tools = tools
        _loaded = True  # 仅成功后置位: 失败保持可重试状态
        logger.info(
            "MCP 工具挂载成功 | server=%s | tools=%s",
            settings.mcp_server_url,
            [t.name for t in _mcp_tools],
        )
    except Exception as e:
        # server 不在线 → 降级: 本地工具语义重合, 图不受影响;
        # 不置 _loaded —— 下次调用重试(H8)
        logger.warning(
            "MCP server 不可用, 跳过挂载(下次调用将重试): %s", str(e)[:150]
        )
        _client = None
        _mcp_tools = []
    return _mcp_tools


async def close_mcp() -> None:
    """清理 MCP 挂载状态(应用关闭时调用)。

    langchain-mcp-adapters 0.3.x 的会话按调用临时建立并自行回收,
    无进程级连接需要显式关闭;这里只复位缓存供测试/重启复用。
    """
    global _mcp_tools, _client, _loaded
    _client = None
    _mcp_tools = []
    _loaded = False
