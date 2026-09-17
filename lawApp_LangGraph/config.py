"""运行时配置中心 — 全部调参与环境变量的单一入口。

字段与同名环境变量(大写)一一对应, 优先级: 进程 env > .env 文件 > 默认值。
设计来源: docs/superpowers/specs/2026-09-11-engineering-foundation-design.md

明确不收拢(见 spec §4.3):
    LLM 温度/max_tokens  — 提示词工程参数, 保持代码字面量
    HF_ENDPOINT          — 进程启动期生效, 留在 RAG_program.py
"""

from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


# 仓库根下的 data/。用 __file__ 计算, 不含盘符, 换机器/换盘符均可用
_DATA_DIR = Path(__file__).resolve().parents[1] / "data"


class Settings(BaseSettings):
    """全量运行时配置, env 同名覆盖示例: RECURSION_LIMIT=80 即生效。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ============ 图执行 ============
    # LangGraph 超步上限(A3 最坏路径约 42 超步, 60 留余量)
    recursion_limit: int = 60
    # 工具调用总数上限, 防无限重规划
    max_rounds: int = 10
    # 入口澄清轮数上限
    max_clarify_rounds: int = 5
    # 连续失败触发降级询问的阈值
    error_streak_threshold: int = 2

    # ============ 评估阈值(CRAG 三档) ============
    correct_threshold: float = 0.5
    incorrect_threshold: float = 0.2
    min_quality_docs: int = 3

    # ============ LLM ============
    deepseek_pro_model: str = "deepseek-reasoner"
    deepseek_flash_model: str = "deepseek-chat"
    deepseek_api_key: Optional[str] = None
    deepseek_base_url: str = "https://api.deepseek.com"

    # ============ 检索: 嵌入 / 重排序 / BM25 ============
    # env 名为 MEMORY_EMBED_MODEL(历史命名, 与 db_tools 记忆索引共用)
    memory_embed_model: str = "BAAI/bge-large-zh-v1.5"
    rerank_model: str = "BAAI/bge-reranker-large"
    embed_dim: int = 1024
    # "0" 禁用(保持原字符串语义, 非 bool)
    rerank_enabled: str = "1"
    # 预计算 BM25 参数, env BM25_PATH 可覆盖
    bm25_path: Optional[str] = str(_DATA_DIR / "bm25_law_params.json")

    # ============ 数据文件 ============
    # 法律文档目录(法条 TXT + 案例 MD), env DOCUMENTS_DIR 可覆盖
    documents_dir: str = str(_DATA_DIR / "Documents")

    # ============ 检索: Pinecone ============
    pinecone_index_name: str = "pinecone-test-lawapp"
    pinecone_api_key: Optional[str] = None
    pinecone_cloud: str = "aws"
    pinecone_region: str = "us-east-1"

    # ============ 日志 ============
    log_dir: str = "./logs"
    log_console_level: str = "DEBUG"
    log_file_level: str = "INFO"

    # ============ MCP ============
    mcp_server_url: str = "http://127.0.0.1:9381/mcp"
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 9381
    # "0"/"false"/"no" 禁用(保持原字符串语义, 非 bool)
    mcp_tools_enabled: str = "1"

    # ============ DB / 持久化 ============
    checkpoint_backend: str = "auto"
    database_url: Optional[str] = None
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "Law_app"
    db_user: str = "postgres"
    db_password: str = "postgres"
    db_pool_max: int = 10


settings = Settings()
