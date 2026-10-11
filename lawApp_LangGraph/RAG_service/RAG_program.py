import hashlib
import os
import re
import time
from typing import Any

from dotenv import load_dotenv
from langchain_community.document_loaders import TextLoader
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)
from pinecone import Pinecone, ServerlessSpec
from pinecone_text.sparse import BM25Encoder

from lawApp_LangGraph.config import settings
from lawApp_LangGraph.FastAPI.logging import rag as rag_log

load_dotenv()

# ---- HuggingFace 镜像 & 下载配置 ----
# 在加载任何 HF 模型之前设置, 避免被镜像网络问题打断
_hf_endpoint = os.getenv("HF_ENDPOINT", "https://huggingface.co")
os.environ["HF_ENDPOINT"] = _hf_endpoint
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "300")
os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "120")

rag_log.debug(
    "HuggingFace 镜像配置",
    detail=f"HF_ENDPOINT={_hf_endpoint}",
)


class RAG_service:
    def __init__(
        self,
        index_name: str,
        api_key: str,
        cloud: str,
        region: str,
        dimension: int = 1024,
    ):
        """
        创建初始化类
        包含:
        初始化Pinecone
        md文档分割工具
        递归分块(段落 句子)
        BGE向量化
        BM25稀疏矩阵向量化

        :param index_name:
        :param api_key:
        :param cloud:
        :param region:
        :param dimension:
        """
        self.index_name = index_name
        self.api_key = api_key
        self.cloud = cloud
        self.region = region
        self.dimension = dimension
        self.pc = Pinecone(api_key=api_key)
        self.index = None

        # 初始化工具
        headers_to_split_on = [("#", "Header_1")]

        # md文档分割
        self.md_splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=headers_to_split_on
        )
        # 段落句子分割
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=512,
            chunk_overlap=50,
            separators=["\n\n", "\n", ".", "；", " "],
            add_start_index=True,
        )

        # 重排经 RAG_service.embedder 单例(懒加载), __init__ 不再预载 GB 级模型

        # 稀疏向量（BM25 参数随仓库 data/ 分发，可经 BM25_PATH 覆盖）
        self.bm25 = BM25Encoder().load(settings.bm25_path)

        # 密集向量
        model_name = "BAAI/bge-large-zh-v1.5"
        self.embeddings = HuggingFaceEmbeddings(
            model_name=model_name, encode_kwargs={"normalize_embeddings": True}
        )

    def create_index(self, wait_for_completion: bool = True) -> bool:
        """
        创建索引
        创建混合索引
        指定的索引方式
        :param wait_for_completion:
        :return:
        """

        # 混合索引的强制要求：metric 必须为 dotproduct,vector_type 为 dense
        target_metric = "dotproduct"

        # 检查索引是否存在
        if not self.pc.has_index(self.index_name):
            print(f"正在创建混合检索索引: {self.index_name}...")
            self.pc.create_index(
                name=self.index_name,
                dimension=self.dimension,
                metric=target_metric,
                spec=ServerlessSpec(cloud=self.cloud, region=self.region),
                vector_type="dense",
            )
        else:
            print(f"索引 '{self.index_name}' 已存在.")
        if wait_for_completion:
            # L20: 就绪轮询加总超时与失败态直判, 不再无限转
            # (Pinecone IndexModel.status 为 IndexStatus, ready/state 两字段;
            #  state 可能取值 Ready/Initializing/InitializationFailed/...)
            deadline = time.monotonic() + 600  # 总超时 600s
            while True:
                status = self.pc.describe_index(self.index_name).status
                if status.get("ready", False):
                    break
                state = str(status.get("state", "") or "").lower()
                # 明确失败态直接抛, 不再傻等
                if state in (
                    "failed",
                    "initialization_failed",
                    "initializationfailed",
                    "error",
                ):
                    raise RuntimeError(f"索引创建失败: state={state}")
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"索引就绪等待超时(600 秒): {self.index_name}"
                    )
                time.sleep(2)

        self.index = self.pc.Index(self.index_name)
        print(f"索引 '{self.index_name}' 已就绪.")
        return True

    def get_index_stats(self):
        stats = self.index.describe_index_stats()
        print("当前索引状态:", stats)
        return stats

    def get_Documents(self, file_path: str) -> list[Any] | None:
        """
        添加文本到数据库中需要:
        加载文本,
        文本分块,

        具体实施:
        (正则表达式)
        将清洗好的文件加载
        以每一个案例为单位
        先提取元数据
        (分块)
        从[基本案情]到最后的内容提取
        以句子为单位,合成一大段

        数据清洗:
        metadata:{year,case_number,case_cause,chunk_text}


        :param file_path:str
        :return 符合输入格式的列表
        """

        loader = TextLoader(file_path, encoding="utf-8")

        # 插入Pinecone数据容器
        Pinecone_records = []

        # 默认读取的为Document形式
        documents = loader.load()

        # L6: 空文档(空文件/加载异常)裸下标 documents[0] 会 IndexError →
        # 显式校验抛错(错误风格对齐本文件 RuntimeError 中文文案)
        if not documents:
            raise ValueError(f"文档加载结果为空, 无法解析: {file_path}")

        # 统一换行符 (\r\n → \n), 避免跨平台正则匹配问题
        raw_text = documents[0].page_content
        raw_text = raw_text.replace("\r\n", "\n").replace("\r", "\n")

        # 生成文件唯一标识 (取路径 MD5 前 8 位)
        file_hash = hashlib.md5(file_path.encode()).hexdigest()[:8]

        # 按一级标题分割成多个案例
        articles = self.md_splitter.split_text(raw_text)

        # 获取元数据和正文内容
        for DocuID, article in enumerate(articles):
            print(f">> 第{DocuID}篇文章获取中...")
            # 元数据容器
            metadata = {}

            # 提取年份：假设文件名或路径包含 202X
            year_match = re.search(r"20\d{2}", article.page_content)
            metadata["year"] = year_match.group(0) if year_match else "Unknown"

            # 提取裁判书字号：匹配如（2023）最高法民终...号
            case_num_pattern = (
                r"裁判书字号\s+((?:(?!裁判书字号)[\s\S])+?法院[\s\S]+?书)"
            )
            case_num_match = re.search(case_num_pattern, article.page_content)

            metadata["case_number"] = (
                case_num_match.group(1).strip() if case_num_match else "未识别"
            )

            # 提取案由：通常在字号之后,或者是特定的段落
            case_cause_pattern = r"案由[:：]\s*([\u4e00-\u9fa5]+)"
            cause_match = re.search(case_cause_pattern, article.page_content)

            metadata["case_cause"] = cause_match.group(1) if cause_match else "通用"

            # 最终的metadata示例: 'metadata': {'case_cause': ,'case_number': , 'chunk_index': 2,'chunk_text': }

            # 提取基本案情
            facts_pattern = r"【基本案情】\s*([\s\S]+?)(?=\n【|$)"
            facts_match = re.search(facts_pattern, article.page_content)

            if not facts_match:
                continue
            # 获取捕获组内容（不含【基本案情】）
            raw_content = facts_match.group(1)
            # 去除空格、换行、制表符等所有空白字符,以及 # 符号
            facts_cleaned = re.sub(
                r"\n+", "\n", raw_content
            ).strip()  # 去除所有空白（空格、换行等）
            facts_cleaned = facts_cleaned.replace("#", "")  # 去除所有 # 字符

            print("元数据和原文解析完毕...")

            chunks = self.text_splitter.split_text(facts_cleaned)

            for i, chunk in enumerate(chunks):
                print(f"第{i}个记录创建中...")
                # 独有的
                record_id = f"lawCase_{file_hash}_{DocuID}_chunk{i}"

                # 密集向量
                dense_vector = self.embeddings.embed_query(chunk)

                # 返回 {"indices": [...], "values": [...]}
                sparse_vector = self.bm25.encode_documents(chunk)

                # 过滤空稀疏向量 (BM25 对极短文本可能返回空)
                if not sparse_vector["values"] or not sparse_vector["indices"]:  # type: ignore
                    continue

                """
                添加内容: id 向量数据 元数据:{年份 判决书 案由 文档切片}
                符合Pinecone的输入格式
                """
                record = {
                    "id": record_id,
                    "values": dense_vector,  # 稠密向量列表
                    "sparse_values": sparse_vector,  # 稀疏向量字典
                    "metadata": {
                        **metadata,
                        "chunk_index": i,
                        "chunk_text": chunk,
                    },
                }
                Pinecone_records.append(record)

                print("记录插入完毕")

        return Pinecone_records

    def add_document(
        self,
        records: list,
        namespace: str,
        batch_size: int = 50,
        pause: float = 1.0,
        max_retries: int = 3,
        show_stats: bool = False,
    ):
        """分批 upsert 到 Pinecone, 批次间暂停 pause 秒避免 API 限流。

        :param records: 待上传的记录列表
        :param namespace: Pinecone 命名空间
        :param batch_size: 每批上传条数
        :param pause: 批次间等待秒数
        :param max_retries: 单批失败最大重试次数
        :param show_stats: 是否在完成后打印索引统计
        """
        if self.index is None:
            raise RuntimeError("索引未初始化, 请先调用 create_index()")

        if not records:
            print("  记录列表为空, 跳过上传")
            return True

        total = len(records)
        success_count = 0
        fail_count = 0

        for i in range(0, total, batch_size):
            batch = records[i : i + batch_size]
            uploaded = min(i + batch_size, total)

            for attempt in range(1, max_retries + 1):
                try:
                    self.index.upsert(vectors=batch, namespace=namespace)
                    success_count += len(batch)
                    print(f"  已上传 {min(uploaded, total)}/{total} 条")
                    break
                except Exception as e:
                    if attempt < max_retries:
                        wait = 2**attempt
                        print(
                            f"  批次 [{i}-{uploaded}] 失败, {wait}s 后重试 ({attempt}/{max_retries}): {e}"
                        )
                        time.sleep(wait)
                    else:
                        fail_count += len(batch)
                        print(f"  批次 [{i}-{uploaded}] 最终失败: {e}")

            if uploaded < total:
                time.sleep(pause)

        print(f"  上传完成: 成功 {success_count} 条, 失败 {fail_count} 条")
        if show_stats:
            self.get_index_stats()
        return fail_count == 0

    def dense_recall(self, query: str, namespace: str, top_k: int) -> list:
        """稠密路独立召回(BGE 嵌入 → 纯向量查询).

        Args:
            query: 查询文本。
            namespace: Pinecone 命名空间。
            top_k: 召回条数(≥1)。

        Returns:
            Pinecone Match 列表(按本路余弦得分降序), 供 fusion.rrf_fuse 消费。

        Raises:
            RuntimeError: 索引未初始化。
        """
        if self.index is None:
            raise RuntimeError("索引未初始化, 请先调用 create_index()")
        dense_vec = self.embeddings.embed_query(query)
        res = self.index.query(
            vector=dense_vec,
            namespace=namespace,
            top_k=max(1, int(top_k)),
            include_metadata=True,
        )
        return list(res.matches)

    def sparse_recall(self, query: str, namespace: str, top_k: int) -> list:
        """稀疏路独立召回(BM25 编码 → 纯稀疏查询).

        Args/Returns/Raises 契约与 dense_recall 一致, 区别仅在向量形态:
        encode_queries 产出 {indices, values} 稀疏向量。
        """
        if self.index is None:
            raise RuntimeError("索引未初始化, 请先调用 create_index()")
        sparse_vec = self.bm25.encode_queries(query)
        res = self.index.query(
            sparse_vector=sparse_vec,
            namespace=namespace,
            top_k=max(1, int(top_k)),
            include_metadata=True,
        )
        return list(res.matches)
