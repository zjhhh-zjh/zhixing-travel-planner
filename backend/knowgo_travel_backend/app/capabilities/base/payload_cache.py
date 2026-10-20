"""
两种缓存（泛型，四类共用）：

- PayloadCache：提交侧候选缓存——查询侧下发卡片时 store，提交侧校验时 get，比对"选的在不在候选里"；
- SignatureCache：段签名查询缓存——同一批段第二次访问直接复用，避免重复打外部服务。

两者都用 Generic[T] 参数化 Payload 类型（TrainMultiSegmentPayload / HotelMultiSegmentPayload…），
一套代码装四种 payload，类型检查器还能帮你查错。
"""
from typing import Callable, Generic, Optional, TypeVar

from app.utils.logger import app_logger

# 泛型 T：某个 capability 的 Payload 类型
T = TypeVar("T")


class PayloadCache(Generic[T]):
    """提交侧候选缓存：按 cache_key（= conversation_id）分桶，多会话并发互不污染

    容量超过 capacity 时踢掉最早写入的一条（与 SignatureCache 同式）；
    每会话仅一条（store 覆盖写），上限远高于日常并发会话数。
    """

    _DEFAULT_KEY = "_"

    def __init__(self, name: str, capacity: int = 128):
        self.name = name
        self.capacity = capacity
        self._data: dict[str, T] = {}

    def get(self, cache_key: Optional[str] = None) -> Optional[T]:
        """取某会话最近下发的候选；不存在返回 None"""
        return self._data.get(cache_key or self._DEFAULT_KEY)

    def store(self, payload: T, cache_key: Optional[str] = None) -> None:
        """写入本会话的候选（供后续提交侧校验）"""
        self._data[cache_key or self._DEFAULT_KEY] = payload
        if len(self._data) > self.capacity:       # 简单容量控制：超过时踢掉最早写入的一条
            self._data.pop(next(iter(self._data)))


class SignatureCache(Generic[T]):
    """段签名查询缓存：同一会话、同一批段第二次访问直接复用，避免重复打外部服务

    is_complete 回答"这份 payload 值不值得缓存"，定义权交给调用方，四类的口径并不相同：
    - 高铁：每一段都有候选才缓存——某段偶发为空若被缓存，用户重试会永远命中这份空快照（"空段死锁"）；
    - 航班 / 自驾 / 住宿：只要有一段有结果就缓存——没有机场码之类的段本来就查不出来，重试也没用。
    容量超过 capacity 时，踢掉最早写入的一条。
    """

    def __init__(self, name: str, capacity: int = 32):
        self.name = name
        self.capacity = capacity
        self._data: dict[str, T] = {}

    def _make_key(self, conversation_id: str, sig: str) -> str:
        return f"{conversation_id}#{sig}"

    def get(
            self,
            conversation_id: str,
            sig: str,
            is_complete: Callable[[T], bool] = lambda _p: True,
    ) -> Optional[T]:
        """取缓存：命中且 is_complete 判定通过才复用（不通过当没命中，留给重试）"""
        cached = self._data.get(self._make_key(conversation_id, sig))
        if cached is None:
            return None
        # ★ 读：不值得复用的当没命中，留给重试
        if not is_complete(cached):
            app_logger.debug(f"[{self.name}] 缓存命中但不完整，不复用")
            return None
        app_logger.info(f"🎯 [{self.name}] 命中段签名缓存")
        return cached

    def store(
            self,
            conversation_id: str,
            sig: str,
            payload: T,
            is_complete: Callable[[T], bool] = lambda _p: True,
    ) -> None:
        """写入缓存：只有 is_complete 判定通过才写"""
        # ★ 写：值不值得缓存由调用方的 is_complete 决定（高铁 all，其余三类 any）
        if not is_complete(payload):
            app_logger.warning(f"⚠️ [{self.name}] payload 不值得缓存，不写签名缓存（留给下次重试）")
            return
        self._data[self._make_key(conversation_id, sig)] = payload
        if len(self._data) > self.capacity:      # 简单容量控制：超过时踢掉最早写入的一条
            self._data.pop(next(iter(self._data)))