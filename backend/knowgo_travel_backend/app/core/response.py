"""
统一响应格式与全局异常处理

前端 request.ts 依赖两点契约：
1. 响应体统一为 {code, msg, data} 包裹结构
2. HTTP 状态码语义：200 成功 / 401 跳登录 / 422 参数不对 / 5xx 服务器错误
"""
from typing import Any
import json
from pydantic import BaseModel
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from app.utils.logger import app_logger


class ResponseModel(BaseModel):
    code: int = 200
    msg: Any = "SUCCESS"
    data: Any | None = None


def response(
    data: Any = None,  # 返回的数据，可以是任意类型，也可以是 None
    code: int = 200,  # → 状态码，默认 200
    msg: Any = "SUCCESS",  # 提示消息，默认 "SUCCESS"
) -> JSONResponse:
    """统一响应包装：{code, msg, data}，HTTP 状态码与 code 同步"""
    if data is None:
        data = []
    payload = ResponseModel(code=code, msg=msg, data=data).model_dump()
    return JSONResponse(content=payload, status_code=code)

# 全局异常处理中间件：兜底 500，避免裸堆栈直接抛给前端
async def global_http_middleware(request: Request, call_next: Any):
    try:
        return await call_next(request)
    except Exception as err:
        app_logger.exception("全局异常")  # ★ 完整堆栈进日志，是排查线上问题的唯一依据
        return response([], 500, str(err))  # ★ 只把一句摘要返回前端，不暴露文件路径/内部函数


# 全局参数校验器：RequestValidationError → 422 + 中文提示
async def validation_exception_handler(request: Request, exc: Any):
    first_error = exc.errors()[0]
    msg = first_error["msg"]
    if msg in ["Field required"]:
        msg = "缺少必传参数"
    return response(code=422, msg=msg, data=[])


# 全局响应包裹中间件：让所有 JSON 接口统一返回 {code, msg, data}
_SKIP_PATH_PREFIXES = ("/docs", "/redoc", "/openapi.json", "/image")


def _rebuild(resp: Any, raw: bytes) -> Response:
    """用已消费的原始字节重建响应

    body_iterator 只能消费一次，取过字节后不能再返回原 resp（流已空）；
    这里保头重建，content-length 交给框架按新 body 重算。
    """
    headers = {k: v for k, v in resp.headers.items() if k.lower() != "content-length"}
    return Response(content=raw, status_code=resp.status_code, headers=headers)


def _wrapped(resp: Any, data: Any, code: int, msg: Any) -> JSONResponse:
    """构造 {code,msg,data} 包裹响应，并原样保留原响应头

    必须保留原响应头：CORSMiddleware 注册在最内层，`Access-Control-Allow-Origin`
    等 CORS 头是挂在原响应上的；若直接 `response()` 新建响应会把这些头丢掉，
    浏览器随即报 "No 'Access-Control-Allow-Origin' header is present"。
    """
    if data is None:
        data = []
    payload = ResponseModel(code=code, msg=msg, data=data).model_dump()
    headers = {k: v for k, v in resp.headers.items() if k.lower() != "content-length"}
    return JSONResponse(content=payload, status_code=code, headers=headers)


async def wrap_json_middleware(request: Request, call_next: Any):
    """统一响应契约：把 JSON 响应包成 {code,msg,data}（前端 request() 只解一层）

    三条跳过规则（命中任意一条即原样放行）：
    1. 路径属文档/静态资源（/docs、/redoc、/openapi.json、/image）
    2. content-type 非 application/json（SSE 的 text/event-stream、文件流）
    3. 响应体已是 {code,msg,data}（response() 产出，避免二次包裹）
    错误响应：HTTP 状态码保持原值（401 仍 401、422 仍 422），msg 取 FastAPI 的 detail
    """
    # ★ 规则 1：文档/静态资源路径直接放行
    if request.url.path.startswith(_SKIP_PATH_PREFIXES):
        return await call_next(request)

    resp = await call_next(request)
    # ★ 规则 2（流式的命脉）：text/event-stream 在这里原样放行，不读 body、不包裹
    if "application/json" not in resp.headers.get("content-type", ""):
        return resp

    # 注意：经 app.middleware("http") 注册后，call_next 返回的是 _StreamingResponse，
    # 它没有 .body 属性（不能像直接调 ASGI app 那样读 resp.body），
    # 必须主动消费 body_iterator 才能拿到原始 JSON 字节。
    # ★ body_iterator 只能消费一次：读完原 resp 就成了空壳，下面只能用 raw 重建
    raw = b"".join([chunk async for chunk in resp.body_iterator])
    try:
        payload = json.loads(raw)
    except Exception:
        return _rebuild(resp, raw)

    # ★ 规则 3：已是 {code,msg,data} 的原样放行，防二次包裹
    if isinstance(payload, dict) and "code" in payload and "data" in payload:
        return _rebuild(resp, raw)

    if resp.status_code < 400:
        return _wrapped(resp, payload, resp.status_code, "SUCCESS")

    msg = payload.get("detail") if isinstance(payload, dict) else None
    return _wrapped(resp, [], resp.status_code, msg or "请求失败")