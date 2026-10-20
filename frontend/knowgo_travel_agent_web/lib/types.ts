/**
 * 全局 TypeScript 类型定义
 * 与后端 zhixing 接口字段严格对齐
 */

/** 用户信息（登录/注册接口返回的 user 字段） */
export interface User {
  id?: string | number;
  username?: string;
  email?: string;
  /** 后端可能返回的其他字段 */
  [key: string]: unknown;
}

/** 登录 / 注册接口统一响应体 */
export interface AuthResponse {
  access_token: string;
  user: User;
}

/** 对话（行程）列表项 */
export interface Conversation {
  id: string;
  title: string;
  /** 展示更新时间（格式如 8月11日） */
  updated_at?: string;
  created_at?: string;
}

/** 消息角色 */
export type MessageRole = 'user' | 'assistant';

/** 消息类型：normal 普通文本 / loading 加载占位 */
export type MessageKind = 'normal' | 'loading';

/** 聊天消息（前端渲染态，比后端多 id/kind 字段） */
export interface ChatMessage {
  id: string;
  role: MessageRole;
  content: string;
  kind: MessageKind;
  /** 行程清单 id：来自 SSE itinerary_plans success 帧 / 历史 extra_info，可点击查看 [MERGE] */
  planId?: string;
  /** 清单生成中标记：来自 pending 帧（success 前按钮显示"生成中"态）[MERGE] */
  planPending?: boolean;
  /** [MERGE] 内嵌 UI Skill 载荷：来自 SSE skill_ui 帧，MessageItem 按 skill_name 渲染组件 */
  skillUi?: SkillUiFrame;
  /** [MERGE] 模型等待态：true 时气泡内展示波浪动画（首个分片到达前 / 分片间歇静默期） */
  waiting?: boolean;
  /** 本地时间戳，用于 React key 稳定性与排序 */
  createdAt: number;
}

/**
 * 统一事件流帧 [MERGE]：与 knowgo_travel app/core/events.py 严格对齐
 * SSE 帧 = data: {"role", "content", "code"?}，role 任何一侧不得私自变更
 */
export type StreamEventRole =
  | 'assistant'        // 流式文本 chunk
  | 'assistant_reset'  // 撤销本回合已推送文本（后端择优回退时下发，前端清空后等待重推）
  | 'skills'           // 技能执行状态 {skill_name,title,desc}（网页端不渲染，忽略）
  | 'skill_ui'         // [MERGE] 结构化 UI Skill 载荷 {skill_name,data}：内嵌渲染交互组件
  | 'itinerary_plans'  // 行程清单状态 {status,id,message}
  | 'done'             // 本轮结束（恒为末帧）
  | 'error'            // 业务错误
  | 'end';             // 连接级终止（如鉴权失败 code=401）

export interface StreamEvent {
  role: StreamEventRole;
  content?: unknown;
  code?: number;
}

/** SSE skills 帧的 content 结构（网页端用于状态提示） */
export interface SkillEvent {
  skill_name: string;
  title: string;
  desc: string;
}

/**
 * [MERGE] SSE skill_ui 帧的 content 结构：Agent 下发的 UI Skill 载荷。
 * data 为该 skill 自己的出参结构（如 train_multi_segment 的 TrainMultiSegmentSkillData），
 * 语义由 skill_name 决定，渲染时按注册表取组件并做窄化。
 */
export interface SkillUiFrame {
  skill_name: string;
  data: unknown;
  /** [MERGE] 修复 167：已提交账本（后端提交接口写回 extra_info，随历史帧回放下发）；
   *  存在即表示该卡片已提交，前端须渲染为锁定态并用 selections 复原各段选中项 */
  submitted?: { selections?: Record<string, string> };
}

/** SSE itinerary_plans 帧的 content 结构 */
export interface ItineraryPlanEvent {
  status: 'pending' | 'success' | 'error';
  id: string;
  message: string;
}

// ==================== 行程清单契约（knowgo_travel TravelItinerary） ====================

/** 景点 */
export interface ScenicSpot {
  toponym: string;
  toponym_desc: string;
  toponym_image: string;
  toponym_image_list: string[];
  longitude?: number | null;
  latitude?: number | null;
}

/** 时段安排 */
export interface ItineraryItem {
  time: string;
  scenic_spots_list: ScenicSpot[];
}

/** 一天 */
export interface ItineraryDay {
  id: string;
  day: string;
  itinerary: ItineraryItem[];
  /** [MERGE] 每段路线平铺坐标 [lat,lng,...]（高德方向API），空数组=前端回退直线 */
  route_polylines?: number[][];
}

/** 行程清单（GET /api/v1/itinerary-plans/{planId} 返回的 data） */
export interface TravelItinerary {
  title: string;
  itinerary_list: ItineraryDay[];
}

/** 历史消息（GET /api/v1/messages?conversation_id=xxx 的 messages[] 元素） */
export interface HistoryMessage {
  role: 'user' | 'assistant';
  content: string;
  /** assistant 富字段：original 全文 / itinerary_plans 行程帧回放 / skill_ui 卡片帧回放 [MERGE] */
  extra_info?: {
    original?: string;
    itinerary_plans?: ItineraryPlanEvent;
    /** [MERGE] 修复 154：卡片帧回放（后端 _SkillUiRecorder 落库），历史消息挂回 skillUi 重渲染 */
    skill_ui?: SkillUiFrame;
    [key: string]: unknown;
  };
}

/** 后端错误响应体（FastAPI 风格） */
export interface ApiErrorBody {
  detail?: string;
  message?: string;
  [key: string]: unknown;
}
