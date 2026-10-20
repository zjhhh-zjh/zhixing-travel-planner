"""
MCP 客户端管理器
统一管理所有 MCP 服务连接
"""
import asyncio
import os
import sys
import threading
import time  # [MERGE] 并行验证：MCP 工具调用耗时统计
from typing import Optional, List
from dotenv import load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient
from app.utils.logger import app_logger


load_dotenv()


# [MERGE] Windows 双循环兼容：
#   主循环=Selector（psycopg 异步连接池硬性要求，connection_async.py 会拒绝 Proactor），
#   但 Selector 在 Windows 上不支持子进程（NotImplementedError），而 stdio MCP 依赖子进程。
#   故 MCP 会话全部运行在专用 Proactor 后台线程循环上，工具调用跨线程调度。
_WIN = sys.platform == "win32"
_mcp_loop: Optional[asyncio.AbstractEventLoop] = None
_mcp_loop_lock = threading.Lock()


def _get_mcp_loop() -> asyncio.AbstractEventLoop:
    """惰性创建 MCP 专用 Proactor 事件循环（后台守护线程常驻运行）"""
    global _mcp_loop
    if _WIN and (_mcp_loop is None or _mcp_loop.is_closed()):
        with _mcp_loop_lock:
            if _mcp_loop is None or _mcp_loop.is_closed():
                _mcp_loop = asyncio.ProactorEventLoop()
                threading.Thread(
                    target=_mcp_loop.run_forever, daemon=True, name="mcp-proactor-loop"
                ).start()
    return _mcp_loop


async def _run_on_mcp_loop(coro):
    """把 MCP 相关协程调度到 Proactor 线程循环执行；非 Windows 原地执行"""
    if not _WIN:
        return await coro
    return await asyncio.wrap_future(
        asyncio.run_coroutine_threadsafe(coro, _get_mcp_loop())
    )


class MCPClientManager:
    """
    MCP 客户端管理器（单例模式）
    """

    _instance: Optional['MCPClientManager'] = None
    _configs: Optional[dict] = None          # [MERGE] 服务名 → 连接配置（初始化标志）
    _clients: Optional[dict] = None          # [MERGE] 服务名 → 独立客户端（按服务隔离）
    _tools: Optional[List] = None
    _lock = asyncio.Lock()

    # 项目根目录（用于 stdio 服务）
    PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))

    # 环境变量（追加 PYTHONPATH）
    ENV_VARS = os.environ.copy()
    ENV_VARS["PYTHONPATH"] = PROJECT_ROOT + os.pathsep + ENV_VARS.get("PYTHONPATH", "")

    # 服务器配置
    SERVER_CONFIGS = {
        # ========== 自建服务（stdio） ==========
        "weather": {
            # 用 Python解释器 来启动这个 MCP 服务
            "command": "python",
            # -m：Python 官方参数 → 以「模块方式」运行一个 Python 文件
            # app.mcp.servers.weather_server：你的天气 MCP 服务的模块路径
            # 对应项目里的文件：app/mcp/servers/weather_server.py
            "args": ["-m", "app.mcp_core.servers.weather_server"],
            # 通行方式
            "transport": "stdio",
            # 环境变量
            "env": ENV_VARS,
        },
        "search": {
            "command": "python",
            "args": ["-m", "app.mcp_core.servers.search_server"],
            "transport": "stdio",
            "env": ENV_VARS,
        },

        # ========== 外部服务（HTTP） ==========
        "amap": {
            "url": f"https://mcp.amap.com/mcp?key={os.getenv('AMAP_API_KEY', '')}",
            "transport": "http",
        },
        # 原注释的 modelscope 托管 URL 已失效（initialize 一律 400），改用官方 npm 包 stdio 方式
        "12306-mcp": {
            # Windows 下 npx 实为 npx.cmd，需经 cmd /c 启动，否则子进程解析失败；
            # macOS/Linux 无 cmd，直接以 npx 启动即可（POSIX 下 npx 可执行，无需 cmd 包装）
            "command": "cmd" if _WIN else "npx",
            "args": ["/c", "npx", "-y", "12306-mcp"] if _WIN else ["-y", "12306-mcp"],
            "transport": "stdio",
        },
        "VariFlight-Aviation": {
            "url": f"https://ai.variflight.com/servers/aviation/mcp/?api_key={os.getenv('VARIFLIGHT_API_KEY', '')}",
            "transport": "streamable_http",
        },
        "aigohotel-mcp": {
            "url": "https://mcp.aigohotel.com/mcp",
            "transport": "streamable_http",
            "headers": {
                "Authorization": f"Bearer {os.getenv('AIGOHOTEL_MCP_API')}",
                "Content-Type": "application/json"
            }
        },
        # 百炼联网搜索
        "bailian-websearch": {
            "url": "https://dashscope.aliyuncs.com/api/v1/mcps/WebSearch/mcp",
            "transport": "streamable_http",
            "headers": {
                "Authorization": f"Bearer {os.getenv('DASHSCOPE_API_KEY', '')}",
            },
        }
    }

    @classmethod
    async def get_instance(cls, servers: List[str] = None) -> 'MCPClientManager':
        """获取单例实例"""
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
                    await cls._instance.initialize(servers=servers)
        return cls._instance

    @classmethod
    def reset_instance(cls):
        """重置单例（用于测试）"""
        cls._instance = None

    async def _load_tools(self) -> List:
        """[MERGE] 按服务隔离加载：单个 MCP 服务故障只跳过自身，不拖垮其他服务

        原实现单个 MultiServerMCPClient 把 9 个连接放在一个 TaskGroup 里，
        任一远程主机抖动（间歇性 ConnectError）即全部报废。
        现改为每服务独立客户端 + gather 收集，失败服务记日志返回空列表。
        """
        async def load_one(name: str, client: MultiServerMCPClient) -> List:
            # [MERGE] 失败自动重试一次：远程 MCP 握手受网络抖动影响概率性失败
            # （实测 aigohotel-mcp 连续两次启动失败、单测直连即成功），重试可消除偶发抖动
            for attempt in (1, 2):
                try:
                    return await _run_on_mcp_loop(client.get_tools())
                except Exception as e:
                    if attempt == 1:
                        app_logger.warning(f"⚠️ MCP 服务 {name} 加载失败，2 秒后重试: {e}")
                        await asyncio.sleep(2)
                    else:
                        app_logger.warning(f"⚠️ MCP 服务 {name} 重试仍失败（本轮跳过）: {e}")
                        return []

        clients = {n: MultiServerMCPClient({n: cfg}) for n, cfg in self._configs.items()}
        results = await asyncio.gather(*(load_one(n, c) for n, c in clients.items()))
        self._clients = clients
        tools = [t for group in results for t in group]
        if not tools:
            raise RuntimeError("所有 MCP 服务均不可用")
        app_logger.info(f"✅ MCP 工具隔离加载完成: {len(tools)} 个（"
                        f"{sum(1 for r in results if r)}/{len(clients)} 服务在线）")
        return tools

    async def initialize(self, servers: List[str] = None):
        """
        初始化 MCP 客户端

        Args:
            servers: 要启用的服务列表，默认启用所有
        """
        if getattr(self, "_configs", None) is not None:
            app_logger.warning("⚠️ MCP 客户端已初始化，跳过")
            return

        # 默认启用所有服务
        servers = servers or list(self.SERVER_CONFIGS.keys())
        configs = {k: v for k, v in self.SERVER_CONFIGS.items() if k in servers}

        app_logger.info(f"初始化 MCP: {list(configs.keys())}")
        self._configs = configs

        # 预加载工具 [MERGE]：在 Proactor 线程循环上建立会话（stdio 子进程依赖），按服务隔离加载
        try:
            self._tools = self._wrap_hopping(await self._load_tools())
        except Exception as e:
            app_logger.warning(f"⚠️ 预加载工具失败: {e}")
            self._tools = []

    @staticmethod
    def _wrap_hopping(tools: List):
        """[MERGE] 为每个 MCP 工具的协程包一层跨循环调度（会话绑定在 Proactor 线程循环上）"""
        for t in tools:
            original = getattr(t, "coroutine", None)
            if original is not None:
                async def _hopped(*args, _orig=original, _tool_name=t.name, **kwargs):
                    # [MERGE] 降级处理：远程服务抖动/超时/配额超限时不抛异常，
                    # 而是把失败转成"工具结果"文本喂给模型，由模型向用户转述
                    # （如"酒店信息暂无搜索到"），避免技术报错炸穿到聊天气泡，流程不中断
                    # [MERGE] 并行验证：记录每次调用的起点/终点耗时，
                    # 多个工具的开始时间戳重叠即可证明同轮并行执行生效
                    _t0 = time.perf_counter()
                    app_logger.info(f"⏱️ MCP 工具 {_tool_name} 开始调用")
                    last_exc: Exception = RuntimeError("unknown")
                    # [MERGE] 并发容错：运行期调用失败（如 HTTP 服务被并行请求炸坏会话）先延迟重试一次
                    for attempt in (1, 2):
                        try:
                            result = await _run_on_mcp_loop(_orig(*args, **kwargs))
                            suffix = "（第2次重试成功）" if attempt == 2 else ""
                            app_logger.info(
                                f"⏱️ MCP 工具 {_tool_name} 调用完成，耗时 {time.perf_counter() - _t0:.2f}s{suffix}"
                            )
                            return result
                        except Exception as e:
                            last_exc = e
                            app_logger.warning(
                                f"MCP 工具 {_tool_name} 第 {attempt} 次调用失败（{time.perf_counter() - _t0:.2f}s）: {e}"
                            )
                            if attempt == 1:
                                await asyncio.sleep(1.5)  # 给服务喘息窗口后重试
                    # [MERGE] 自愈：重试仍失败说明连接已被破坏，重置连接缓存，
                    # 下一轮 middleware 重新加载步骤工具时按配置全量重建（免重启后端）
                    MCPClientManager.invalidate()
                    app_logger.warning(
                        f"MCP 工具 {_tool_name} 重试仍失败（{time.perf_counter() - _t0:.2f}s），已重置连接缓存并降级返回"
                    )
                    # [MERGE] 修复：langchain-mcp-adapters 工具声明 response_format='content_and_artifact'，
                    # 降级返回必须也是 (content, artifact) 二元组，否则 LangChain 校验炸出
                    # "a two-tuple of the message content and raw tool output is expected"
                    return (
                        (
                            f"【外部服务暂时不可用】{_tool_name} 本次调用失败（{type(last_exc).__name__}）。"
                            f"请向用户说明该信息暂无搜索到，建议稍后重试，或引导用户跳过此步骤继续行程规划；"
                            f"禁止向用户暴露技术报错细节。"
                        ),
                        None,
                    )
                t.coroutine = _hopped
        return tools

    @classmethod
    def invalidate(cls):
        """连接自愈：运行期调用炸坏会话（如 HTTP 服务被并发请求打出 TaskGroup 异常、
        连接被服务端断开）时，清空客户端与工具缓存（保留 _configs 初始化标志）；
        下次 get_tools() 会按配置全量重建连接，免重启后端即可恢复服务可用。
        """
        cls._clients = None
        cls._tools = None
        if cls._instance is not None:
            cls._instance._clients = None
            cls._instance._tools = None
        app_logger.warning("♻️ MCP 连接缓存已重置（调用重试仍失败），下次获取工具时将重建全部服务连接")

    async def close(self):
        """关闭客户端"""
        if getattr(self, "_configs", None) is not None:
            self._configs = None
            self._clients = None
            self._tools = None
            app_logger.info("MCP 客户端已关闭")

    #充当对外接口(懒加载策略)
    async def get_tools(self) -> List:
        """
        获取所有 MCP 工具

        Returns:
            LangChain 工具列表
        """
        if getattr(self, "_configs", None) is None:
            raise RuntimeError("MCP 客户端未初始化，请先调用 initialize()")

        # 如果已缓存，直接返回
        if self._tools:
            return self._tools

        # 否则重新获取 [MERGE]：同样在 MCP 循环上建立会话并包装跨循环调度（带重试）
        self._tools = self._wrap_hopping(await self._load_tools())
        return self._tools


async def get_mcp_client(servers: List[str] = None) -> MCPClientManager:
    """获取 MCP 客户端管理器实例"""
    return await MCPClientManager.get_instance(servers)