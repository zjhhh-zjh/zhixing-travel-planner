"""
RAG 目的地信息查询工具
把 Advanced RAG Pipeline 包成 Agent 能调的工具
"""
from langchain.tools import tool

from app.rag.pipeline import get_rag_pipeline
from app.utils.logger import app_logger


@tool
def query_destination_info(destination: str, query: str = "") -> str:
    """
    查询目的地的沉淀型知识（景点、攻略、开放时间、路线经验等）

    数据源：项目内建的目的地知识库（不是实时数据）
    检索链路：Advanced RAG（父子分块 + 混合检索 + Multi-Query + LLM 重排 + 缓存）

    参数：
    - destination: 目的地名称，如 "西安"、"成都"
    - query: 具体想问的问题（可选），如 "兵马俑值不值得"、"最佳游玩路线"
        不传就默认查该目的地的综合信息

    返回：
    - 3-5 段相关的攻略片段拼接文本，可直接展示给用户或供进一步推理

    调用时机：
    - 目的地推荐步骤：拿目的地亮点 / 景点介绍
    - 行程生成步骤：拿具体路线 / 开放时间等细节
    """
    app_logger.info(f"[RAG] 查询目的地信息: {destination} | {query!r}")

    # 组装最终 query：把目的地拼进去，让 embedding 能捕获"这是哪个城市"
    final_query = f"{destination} {query}".strip() if query else f"{destination} 旅游攻略"

    # 走 Pipeline
    pipeline = get_rag_pipeline()
    docs = pipeline.retrieve(final_query)

    if not docs:
        return f"知识库里暂无 {destination} 的相关内容"

    # 拼接返回文本（每段前加序号，方便模型引用）
    parts = [f"### 来自知识库的资料（关于 {destination}）", ""]
    for i, doc in enumerate(docs, start=1):
        parts.append(f"**片段 {i}**（来源：{doc.metadata.get('source', 'unknown')}）")
        parts.append(doc.page_content)
        parts.append("")

    return "\n".join(parts)