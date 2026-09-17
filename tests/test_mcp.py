"""
MCP 双向冒烟测试 (A2)

覆盖:
    1. server 侧: mcp_server 三个 @mcp.tool 注册 + stdio 传输可用
    2. 端到端: 以 stdio 子进程拉起 law-search server,
       langchain-mcp-adapters Client 调 search_laws → 走到本地 fetch_laws
       (无数据库 → 优雅降级文案,不抛异常)
    3. agent 自举: stdio 挂载远端工具 → register_mcp_tools 去重注册
    4. 降级: MCP_SERVER_URL 指向无人监听端口 → get_mcp_tools 返回空列表

运行: python -m pytest tests/ -q
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PY = sys.executable  # 与 pytest 同解释器,保证依赖可见


#  1. server 侧工具注册


def test_mcp_server_registers_three_tools():
    from lawApp_LangGraph.mcp_server import mcp

    tools = mcp._tool_manager.list_tools()
    names = {t.name for t in tools}
    assert {"search_laws", "search_cases", "recall_memory"} == names


#  2. 端到端: stdio 子进程 server + adapters client 调用(降级文案)


def _stdio_config() -> dict:
    """stdio 子进程挂载配置。

    env 必须显式传: MCP SDK 默认只继承一份白名单环境变量
    (mcp.client.stdio.DEFAULT_INHERITED_ENV_VARS), 其中不含 HF_HOME。
    一旦丢失, 子进程会改用默认缓存目录去找 BGE 嵌入模型, 找不到便转向
    网络重试, 测试表现为长时间挂起而非报错。HF_HUB_OFFLINE=1 让缓存缺失
    时立刻失败, 不再对着网络重试。
    """
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "USERPROFILE": os.environ.get("USERPROFILE", ""),
        "TEMP": os.environ.get("TEMP", ""),
        "HF_HUB_OFFLINE": "1",
        # 子进程输出中文, 固定编码避免 GBK 解码失败
        "PYTHONIOENCODING": "utf-8",
    }
    for key in ("HF_HOME", "HF_ENDPOINT", "HF_HUB_CACHE"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return {
        "law-search-test": {
            "transport": "stdio",
            "command": PY,
            "args": ["-m", "lawApp_LangGraph.mcp_server_stdio"],
            # 子进程按包名导入, 工作目录必须是仓库根
            "cwd": str(ROOT),
            "env": env,
        }
    }


def test_stdio_end_to_end(monkeypatch):
    """子进程 stdio 拉起 server(复用 mcp_server 的 mcp 实例),client 调 search_laws。

    无数据库 → 工具必须走 fetch_laws 的优雅降级(返回「暂不可用」),不抛异常。
    """
    # mcp_server 的 main() 走 http;测试用 stdio 入口直接 run(transport="stdio")
    # —— 写一个只在测试期存在的子模块入口
    entry = ROOT / "lawApp_LangGraph" / "mcp_server_stdio.py"
    entry.write_text(
        'from lawApp_LangGraph.mcp_server import mcp\n\n'
        'if __name__ == "__main__":\n'
        '    mcp.run(transport="stdio")\n',
        encoding="utf-8",
    )
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient

        async def run():
            # adapters ≥0.1.0 不再支持 async with;构造后直接 get_tools
            client = MultiServerMCPClient(_stdio_config())
            tools = await client.get_tools()
            names = {t.name for t in tools}
            assert {"search_laws", "search_cases", "recall_memory"} <= names

            # search_laws: 库不可用 → 降级文案,不抛
            result = None
            for t in tools:
                if t.name == "search_laws":
                    result = await t.ainvoke({"query": "离婚 财产分割", "top_k": 3})
                    break
            assert result is not None
            # adapters 把 MCP content block 包成 [{'type':'text','text':...}] 列表
            if isinstance(result, list):
                text = "".join(
                    b.get("text", "") for b in result
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            else:
                text = str(result)
            assert text.strip(), f"空结果: {result!r}"
            # 降级或空结果文案,绝不应是异常堆栈
            assert "Traceback" not in text
            try:
                await client.__aexit__(None, None, None)
            except NotImplementedError:
                pass  # 新版 adapters 移除了 CM 支持;会话随 GC 关闭

        asyncio.run(run())
    finally:
        entry.unlink(missing_ok=True)


#  3. agent 自举挂载(经 mcp_client 逻辑;直接验证注册去重)


def test_register_mcp_tools_dedup():
    from langchain_core.tools import tool

    from lawApp_LangGraph.tools import ALL_TOOLS, MCP_TOOLS, register_mcp_tools

    MCP_TOOLS.clear()

    @tool
    def search_laws(query: str) -> str:
        """同名 MCP 工具,应因与本地重名被跳过."""
        return "dup"

    # 同名工具改个名:local 已有 fetch_laws 等,构造真正与本地重名的场景
    # —— register_mcp_tools 的去重键是 t.name,这里直接验证行为:
    # 1) 名字与本地重合 → 跳过; 2) 新名 → 注册
    from lawApp_LangGraph.tools.db_tools import fetch_laws as local_fetch_laws

    dup_tool = local_fetch_laws  # 名字 "fetch_laws" 与 LOCAL_TOOLS 重合

    @tool
    def external_new_tool(query: str) -> str:
        """新外部工具,应被注册."""
        return "ok"

    added = register_mcp_tools([dup_tool, external_new_tool])
    try:
        assert [t.name for t in added] == ["external_new_tool"]
        all_names = {t.name for t in ALL_TOOLS()}
        assert "external_new_tool" in all_names
        # 重名工具未被注册进 MCP_TOOLS
        assert dup_tool not in MCP_TOOLS
    finally:
        MCP_TOOLS.clear()


#  4. 降级: server 不在线 → 空列表不阻塞


def test_mcp_client_degrades_when_server_down(monkeypatch):
    import lawApp_LangGraph.mcp_client as mc

    # settings.mcp_server_url 指向无人监听端口 → get_mcp_tools 返回空列表
    monkeypatch.setattr(mc.settings, "mcp_server_url", "http://127.0.0.1:59999/mcp")
    # 重置模块级缓存以强制重连
    monkeypatch.setattr(mc, "_loaded", False)
    monkeypatch.setattr(mc, "_mcp_tools", [])
    monkeypatch.setattr(mc, "_client", None)

    async def run():
        tools = await mc.get_mcp_tools()
        assert tools == []

    asyncio.run(run())


def test_mcp_client_env_switch(monkeypatch):
    """MCP_TOOLS_ENABLED=0 → 不发起连接直接返回空.

    模块经 settings 单例读取, 故 env 断言用临时 Settings 实例(setenv +
    不 reload 全局单例), 模块行为用 patch settings 属性验证。
    """
    from lawApp_LangGraph.config import Settings

    monkeypatch.setenv("MCP_TOOLS_ENABLED", "0")
    assert Settings(_env_file=None).mcp_tools_enabled == "0"

    import lawApp_LangGraph.mcp_client as mc

    monkeypatch.setattr(mc.settings, "mcp_tools_enabled", "0")
    monkeypatch.setattr(mc, "_loaded", False)
    monkeypatch.setattr(mc, "_mcp_tools", [])
    monkeypatch.setattr(mc, "_client", None)

    async def run():
        assert await mc.get_mcp_tools() == []
        assert mc._client is None  # 未建连接

    asyncio.run(run())
