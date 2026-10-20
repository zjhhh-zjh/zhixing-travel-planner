"""对话内容压缩服务：DeepSeek 压缩长文

压缩结果存 Message.content（会话列表预览），全文存 extra_info.original（前端详情渲染）。
"""
from app.utils.llm_factory import get_deepseek_llm

_COMPRESS_PROMPT = (
    "将以下行程规划内容压缩为 300 字以内的中文摘要，"
    "保留行程天数安排与核心亮点，直接输出摘要正文：\n\n"
)

# 超过该长度才压缩，短文直接存原文
COMPRESS_THRESHOLD = 2000  # ★ 闲聊、短回答根本进不来，挡掉无谓的 LLM 调用


async def compress_content(full_text: str) -> str:
    """压缩长文为摘要"""
    res = await get_deepseek_llm().ainvoke(_COMPRESS_PROMPT + full_text)
    return res.content