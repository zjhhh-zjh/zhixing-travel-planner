/**
 * 统一请求封装
 * - 自动携带 redux 中的 token 鉴权头
 * - 401 自动触发登出回调（由 store 层注册）
 * - 网络异常 / 业务错误统一抛出 ApiError
 * - 所有接口地址、请求方式与原 zhixing.html JS 完全对齐
 */
import type { AuthResponse, Conversation, HistoryMessage, TravelItinerary } from './types';

/** API 基础地址存储键（对应原 #apiBase 输入框） */
const API_BASE_KEY = 'zhixing_api_base';
/** knowgo_travel run_uvicorn.py 默认端口 4000 [MERGE] */
// const DEFAULT_API_BASE = 'http://127.0.0.1:4000';
// 浏览器不能用 0.0.0.0 作为请求目标（仅是服务端监听地址），必须写回环地址 [MERGE]
const DEFAULT_API_BASE = 'http://127.0.0.1:8000';

/** 获取 API 基础地址：优先读取用户配置，回退默认值 */
export function getApiBase(): string {
  try {
    return localStorage.getItem(API_BASE_KEY) || DEFAULT_API_BASE;
  } catch {
    return DEFAULT_API_BASE;
  }
}

/** 设置 API 基础地址（AuthOverlay 折叠面板写入） */
export function setApiBase(base: string): void {
  try {
    localStorage.setItem(API_BASE_KEY, base);
  } catch {
    /* localStorage 不可用时静默失败 */
  }
}

// —— 鉴权 token：由 store 层通过 setToken 同步，避免 api 层直接依赖 redux ——
let accessToken = '';
export function setToken(token: string): void {
  accessToken = token;
}
export function getToken(): string {
  return accessToken;
}

// —— 401 统一处理：store 层注册登出回调 ——
let unauthorizedHandler: (() => void) | null = null;
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}
/** SSE end 帧 code=401 时复用同一登出回调 [MERGE] */
export function notifyUnauthorized(): void {
  unauthorizedHandler?.();
}

/** 统一的业务错误类型：携带 HTTP 状态码与后端 detail */
export class ApiError extends Error {
  status: number;
  detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

/**
 * 核心请求函数
 * @param path 接口路径（如 /api/v1/users/login）
 * @param options fetch 选项，会自动补充 Content-Type 与 Authorization 头
 */
export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  if (!(options.body instanceof FormData)) {
    headers.set('Content-Type', 'application/json');
  }
  if (accessToken) {
    headers.set('Authorization', `Bearer ${accessToken}`);
  }

  let response: Response;
  try {
    response = await fetch(`${getApiBase()}${path}`, { ...options, headers });
  } catch {
    // 网络层异常（断网 / 服务未启动）
    throw new ApiError(0, '网络连接异常');
  }

  if (response.status === 401) {
    // 鉴权失效：触发全局登出
    unauthorizedHandler?.();
    throw new ApiError(401, '登录已过期，请重新登录');
  }

  if (!response.ok) {
    let detail = '请求失败';
    try {
      // 兼容两种错误体：v1 路由 {detail}（FastAPI）与契约路由 {code,msg,data} [MERGE]
      const body = (await response.json()) as { detail?: string; message?: string; msg?: string };
      detail = body.detail || body.message || body.msg || detail;
    } catch {
      /* 响应体非 JSON，使用默认错误文案 */
    }
    throw new ApiError(response.status, detail);
  }

  const body = (await response.json()) as unknown;
  // 后端统一 {code,msg,data} 包裹 [MERGE]：此处只解一层，调用方拿到的仍是原 T 结构
  if (body && typeof body === 'object' && 'code' in body && 'data' in body) {
    return (body as { data: T }).data;
  }
  return body as T;
}

// ==================== 接口方法（与原 JS 完全对齐） ====================

/** 用户注册：POST /api/v1/users/register */
export function registerApi(username: string, email: string, password: string): Promise<AuthResponse> {
  return request<AuthResponse>('/api/v1/users/register', {
    method: 'POST',
    body: JSON.stringify({ username, email, password }),
  });
}

/** 用户登录：POST /api/v1/users/login */
export function loginApi(username: string, password: string): Promise<AuthResponse> {
  return request<AuthResponse>('/api/v1/users/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  });
}

/** 获取全部对话列表：GET /api/v1/conversations */
export async function fetchConversationsApi(): Promise<Conversation[]> {
  const data = await request<Conversation[] | { conversations?: Conversation[] }>(
    '/api/v1/conversations',
  );
  // 兼容后端返回数组或 { conversations: [] } 两种结构
  return Array.isArray(data) ? data : data.conversations || [];
}

/** 新建对话：POST /api/v1/conversations（title 固定「新对话」，后端按首条消息自动命名 [MERGE]） */
export function createConversationApi(): Promise<Conversation> {
  return request<Conversation>('/api/v1/conversations', {
    method: 'POST',
    body: JSON.stringify({ title: '新对话' }),
  });
}

/** 获取单个对话（标题）：GET /api/v1/conversations/{id} */
export function fetchConversationApi(id: string): Promise<Conversation> {
  return request<Conversation>(`/api/v1/conversations/${id}`);
}

/** 获取对话历史消息：GET /api/v1/messages?conversation_id=xxx → {conversation, messages} */
export async function fetchHistoryApi(id: string): Promise<HistoryMessage[]> {
  const data = await request<HistoryMessage[] | { messages?: HistoryMessage[] }>(
    `/api/v1/messages?conversation_id=${encodeURIComponent(id)}`,
  );
  return Array.isArray(data) ? data : data.messages || [];
}

/**
 * [MERGE] 流式接口统一请求：POST + JSON body，返回原始 Response 供调用方读取 body 流。
 * 401 复用全局登出回调（与 request 一致）。
 */
function postStream(path: string, body: unknown): Promise<Response> {
  return new Promise<Response>((resolve, reject) => {
    fetch(`${getApiBase()}${path}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${accessToken}`,
      },
      body: JSON.stringify(body),
    })
      .then((res) => {
        if (res.status === 401) {
          unauthorizedHandler?.();
          reject(new ApiError(401, '登录已过期，请重新登录'));
          return;
        }
        resolve(res);
      })
      .catch(() => reject(new ApiError(0, '网络连接异常')));
  });
}

/**
 * SSE 流式聊天：POST /api/v1/messages [MERGE]（资源式扁平契约，体 {conversation_id, content}）
 * 返回原始 Response，由调用方读取 body 流并增量解析统一事件帧。
 */
export function streamChatApi(
  conversationId: string,
  content: string,
): Promise<Response> {
  return postStream('/api/v1/messages', { conversation_id: conversationId, content });
}

/** 行程清单：GET /api/v1/itinerary-plans/{planId}（data 直接为 TravelItinerary）[MERGE] */
export async function fetchItineraryPlanApi(
  planId: string,
): Promise<TravelItinerary> {
  return request<TravelItinerary>(`/api/v1/itinerary-plans/${planId}`);
}

/** 全国城市列表：GET /api/v1/cities?q=xxx（出发地单选 / 目的地多选数据源，空 q 返回全量）[MERGE] */
export interface CityOption {
  name: string;
  province: string;
}
export function fetchCitiesApi(q = ''): Promise<CityOption[]> {
  return request<CityOption[]>(
    `/api/v1/cities${q ? `?q=${encodeURIComponent(q)}` : ''}`,
  );
}

/**
 * [MERGE] 聊天内嵌 UI Skill 的提交路径表：新增一个 Skill 只需在此登记一条，
 * 提交逻辑（chatSlice 的 submitSkillThunk / MessageItem）完全复用，无需再写分支。
 */
const SKILL_SUBMIT_PATHS: Record<string, string> = {
  train_multi_segment: '/api/v1/skills/train_multi_segment/submit',
  hotel_multi_segment: '/api/v1/skills/hotel_multi_segment/submit',
  // [MERGE] 修复 155：多段航班提交端点（thunk 按 skill_name 取路径，无需额外分支）
  flight_multi_segment: '/api/v1/skills/flight_multi_segment/submit',
  // [MERGE] 修复 156：多段自驾确认端点（只读确认，同样按 skill_name 取路径）
  driving_multi_segment: '/api/v1/skills/driving_multi_segment/submit',
};

/**
 * [MERGE] 提交聊天内嵌 UI Skill 的选择结果（多段高铁 / 多段住宿通用）：
 * POST /api/v1/skills/{skill_name}/submit
 *
 * 后端据此写入段账本（高铁 leg_selections / 住宿 selected_hotels）并把状态机推进到下一步，
 * **随后直接续跑后续编排**，故与 /api/v1/messages 一样返回 SSE 流（原始 Response，
 * 由 chatSlice 的事件管道消费）；只做基础合法性校验（段/候选是否存在），
 * **不做任何日期时间校验**。
 */
export function submitSkillApi(
  skillName: string,
  conversationId: string,
  payload: unknown,
): Promise<Response> {
  const path = SKILL_SUBMIT_PATHS[skillName];
  if (!path) throw new Error(`未登记的 UI Skill：${skillName}`);
  return postStream(path, { conversation_id: conversationId, payload });
}
