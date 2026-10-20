"""
事件流管道

单轮对话的事件管道：Agent 核心是生产者，WS/SSE 适配器是消费者。
生产顺序即契约顺序：skills → assistant* → itinerary_plans → done → (close)。
流生命周期 = 单条消息生命周期（SSE 场景下流关闭即响应结束）。
"""
import asyncio
from app.core.events import AgentEvent

_CLOSE = object()  # ★ 流结束哨兵（独一份的裸对象，只用 is 比较，绝不和业务数据撞车）


class EventStream:
    """生产者 push / 消费者 async for 的异步队列包装"""

    def __init__(self) -> None:
        self._queue: asyncio.Queue = asyncio.Queue()

    def push(self, event: AgentEvent) -> None:
        """投递事件。用 put_nowait（同步），便于在流式回调等同步上下文中调用。"""
        # ★ 同步投递：无界队列永远不满，put_nowait 永不阻塞，同步回调里也能直接调
        self._queue.put_nowait(event)

    def close(self) -> None:
        """生产者宣告投递结束，消费者迭代随之终止"""
        self._queue.put_nowait(_CLOSE)

    def __aiter__(self) -> "EventStream":
        return self

    async def __anext__(self) -> AgentEvent:
        item = await self._queue.get()
        # ★ 用 is 判身份而不是 == 判值：业务数据再像也不会被误判成"收摊"
        if item is _CLOSE:
            raise StopAsyncIteration
        return item