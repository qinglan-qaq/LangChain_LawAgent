from lawApp_LangGraph.tools.tools import (
    get_google_search,
    markdown_to_pdf,
)
from lawApp_LangGraph.tools.rag_tools import (
    retrieve_legal_knowledge,
    evaluate_case_relevance,
    analyze_legal_issue,
)
from lawApp_LangGraph.tools.db_tools import (
    search_memory,
    save_to_memory,
    fetch_laws,
)

# Agent 可用的全部本地工具(静态注册)
LOCAL_TOOLS = [
    search_memory,
    save_to_memory,
    fetch_laws,
    get_google_search,
    markdown_to_pdf,
    retrieve_legal_knowledge,
    evaluate_case_relevance,
    analyze_legal_issue,
]

# MCP 工具(law-search server 挂载后由 mcp_client 填充;默认空)
MCP_TOOLS: list = []


def ALL_TOOLS() -> list:
    """完整工具列表 = 本地工具 + MCP 工具。

    做成函数而非静态列表: MCP 挂载发生在 runtime 装配期,
    图/ToolNode/TOOL_BY_NAME 在 build_graph 时读取此刻的快照。
    """
    return LOCAL_TOOLS + MCP_TOOLS


def register_mcp_tools(tools: list) -> list:
    """注册 MCP 工具(去重: 与本地工具同名的跳过)。返回实际新增列表。"""
    local_names = {t.name for t in LOCAL_TOOLS}
    added = [t for t in tools if t.name not in local_names]
    MCP_TOOLS.extend(added)
    return added
