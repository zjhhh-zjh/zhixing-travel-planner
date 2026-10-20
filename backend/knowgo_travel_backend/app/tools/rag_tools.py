import contextvars
from typing import Optional
from langchain.tools import tool
from langchain_core.tools import ToolException
from app.rag.pipeline import AdvancedRAGPipeline
from app.rag.document_loader import DocumentManager
from app.rag.text_splitter import AdvancedParentDocumentSplitter
from app.rag.vectorstore import VectorStoreManager
from app.utils.logger import app_logger

# ★ 用 ContextVar 存"当前目标城市"（4.2 详解为什么不能用普通全局变量）
_RAG_CITY: contextvars.ContextVar[str] = contextvars.ContextVar("rag_city", default="")


def set_rag_city(city: str) -> None:
    """注入目标城市（并行分支互不串号）"""
    _RAG_CITY.set(str(city or "").strip())


_rag_pipeline: Optional[AdvancedRAGPipeline] = None       # 全局单例：整条 pipeline 造一次复用
_parent_splitter: Optional[AdvancedParentDocumentSplitter] = None


async def _get_rag_pipeline() -> AdvancedRAGPipeline:
    """懒加载整条 RAG 管道（内部就是第 7 章那套父子分块+混合检索+重排+缓存，本章不重讲）"""
    global _rag_pipeline, _parent_splitter
    if _rag_pipeline is None:
        app_logger.info("🔧 初始化 RAG 管道...")
        documents = DocumentManager().load_destination_documents() or []
        _parent_splitter = AdvancedParentDocumentSplitter()
        parent_docs, child_docs = _parent_splitter.split_documents(documents)

        vs_manager = VectorStoreManager()
        try:
            vectorstore = vs_manager.load_vectorstore()
            # ★ 判空校验：Chroma 集合不存在时会静默新建"空集合"而不报错，
            #   只靠 try/except 判断不出"库是空的"，必须显式数条数，为 0 就重建。
            if vectorstore._collection.count() == 0 and child_docs:
                app_logger.warning("⚠️ 向量库为空，重建...")
                try:
                    vectorstore = vs_manager.create_vectorstore(child_docs)
                except Exception as ce:
                    app_logger.error(f"❌ 重建失败（{ce}），降级为仅 BM25")
        except Exception as e:
            app_logger.info(f"📦 向量库加载失败（{e}），创建新的...")
            if child_docs:
                vectorstore = vs_manager.create_vectorstore(child_docs)
            else:
                raise RuntimeError(f"向量库不可用且无子文档可重建: {e}")

        _rag_pipeline = AdvancedRAGPipeline(
            vectorstore=vectorstore, all_documents=child_docs,
            parent_splitter=_parent_splitter, query_strategy="multi_query",
            top_k=3, enable_cache=True,
        )
        app_logger.info("✅ RAG 管道初始化完成")
    return _rag_pipeline


def _format_rag_results(documents: list, query: str) -> str:
    """格式化检索结果 —— ★ 城市限定：只保留正文提到目标城市的文档（4.2 讲为什么）"""
    city = _RAG_CITY.get()
    if city and documents:
        kept = [d for d in documents if city in (d.page_content or "")]   # 剔除不含目标城市的
        if len(kept) != len(documents):
            app_logger.warning(f"⚠️ 已剔除 {len(documents) - len(kept)} 条非「{city}」文档")
        documents = kept
    if not documents:                          # 剔完为空就如实说，绝不拿别的城市内容冒充
        if city:
            return (f"知识库中暂无「{city}」的攻略资料（已按城市过滤）。"
                    "请基于通用旅行常识给建议，且不要编造具体门票价格/开放时间。")
        return f"未找到与「{query}」相关的信息。"

    parts = []
    for i, doc in enumerate(documents, 1):
        content = doc.page_content[:800] + ("..." if len(doc.page_content) > 800 else "")
        parts.append(f"【资料 {i}】\n{content}\n来源：{doc.metadata.get('source', '未知来源')}")
    return "\n\n".join(parts)


# —— 四把工具：结构完全相同，只差 docstring（给模型的说明书）和查询增强词 ——

@tool
async def search_destination_guide(query: str) -> str:
    """从旅游攻略知识库检索目的地信息（景点/门票/开放时间/路线/交通/住宿区域）。
    需要知识库攻略时用它。query 例：'西安兵马俑门票和游玩建议'。"""
    app_logger.info(f"RAG 工具被调用: {query}")
    try:
        pipeline = await _get_rag_pipeline()
        return _format_rag_results(pipeline.retrieve(query), query)   # 攻略最泛，不加增强词
    except Exception as e:
        app_logger.error(f"❌ RAG 检索失败: {e}")
        raise ToolException(f"检索过程中出现错误：{str(e)}")   # 抛给 Agent，让它自己决定怎么办


@tool
async def search_food_recommendations(query: str) -> str:
    """检索目的地美食（特色/必吃/餐厅/美食街/价格）。query 例：'西安特色美食推荐'。"""
    app_logger.info(f"美食检索工具被调用: {query}")
    try:
        pipeline = await _get_rag_pipeline()
        # 查询增强：原 query 后补该领域关键词再检索，让 embedding 更容易命中美食类内容
        return _format_rag_results(pipeline.retrieve(f"{query} 美食 餐厅 小吃"), query)
    except Exception as e:
        app_logger.error(f"❌ 美食检索失败: {e}")
        raise ToolException(f"检索过程中出现错误：{str(e)}")


@tool
async def search_accommodation_info(query: str) -> str:
    """检索目的地住宿（区域推荐/类型特点/价格范围/选择建议）。query 例：'西安住哪个区域好'。"""
    app_logger.info(f"住宿检索工具被调用: {query}")
    try:
        pipeline = await _get_rag_pipeline()
        return _format_rag_results(pipeline.retrieve(f"{query} 住宿 酒店 民宿"), query)
    except Exception as e:
        app_logger.error(f"❌ 住宿检索失败: {e}")
        raise ToolException(f"检索过程中出现错误：{str(e)}")


@tool
async def search_travel_tips(query: str) -> str:
    """检索旅行实用信息（注意事项/避坑/最佳季节/穿衣/消费水平）。query 例：'西安旅游注意事项'。"""
    app_logger.info(f"旅行贴士检索工具被调用: {query}")
    try:
        pipeline = await _get_rag_pipeline()
        return _format_rag_results(pipeline.retrieve(f"{query} 注意事项 建议 提示"), query)
    except Exception as e:
        app_logger.error(f"❌ 旅行贴士检索失败: {e}")
        raise ToolException(f"检索过程中出现错误：{str(e)}")


def get_rag_tools() -> list:
    """把四把工具打包成列表，交给 explore Agent 挂载"""
    return [search_destination_guide, search_food_recommendations,
            search_accommodation_info, search_travel_tips]