/**
 * 聊天切片：对话列表、消息、加载状态、SSE 流式异步 thunk
 *
 * 性能优化点：
 * 1. SSE 增量分段：通过 streamingTargets 注册表直接拿目标 DOM，
 *    用 TextNode 分片追加 + <br> 换行，避免每次 setState 触发整树重绘；
 * 2. 滚动使用 requestAnimationFrame 节流，最多每帧滚动一次；
 * 3. 切换会话时校验 currentConversationId，防止慢请求覆盖新会话数据。
 */
import {
  createAsyncThunk,
  createSlice,
  type PayloadAction,
} from '@reduxjs/toolkit';
import {
  ApiError,
  createConversationApi,
  fetchConversationApi,
  fetchConversationsApi,
  fetchHistoryApi,
  fetchItineraryPlanApi,
  notifyUnauthorized,
  streamChatApi,
  submitSkillApi,
} from '@/lib/api';
import type {
  ChatMessage,
  Conversation,
  HistoryMessage,
  ItineraryPlanEvent,
  MessageRole,
  SkillEvent,
  SkillUiFrame,
  StreamEvent,
} from '@/lib/types';
import type { PersistedChat } from '@/lib/persist';
import { setPlanLoading, showToast, showPlan } from './uiSlice';
import type { RootState, AppDispatch } from '../index';

// ==================== 状态类型 ====================

/** 类型化 thunk 工厂：让 getState() 推断为 RootState、dispatch 兼容 AppDispatch */
const createTypedAsyncThunk = createAsyncThunk.withTypes<{
  state: RootState;
  dispatch: AppDispatch;
}>();

export interface ChatState {
  /** 全部对话（行程）列表 */
  conversations: Conversation[];
  /** 当前会话 ID */
  currentConversationId: string | null;
  /** 当前会话的消息数组 */
  messages: ChatMessage[];
  /** 发送加载锁：防止重复点击重复请求 */
  isLoading: boolean;
  /** SSE 流式进行中 */
  isStreaming: boolean;
  /** 正在流式渲染的助手消息 id */
  streamingMessageId: string | null;
  /** 流式过程中的状态提示文本（skills 执行步骤 / 清单生成中）[MERGE] */
  statusText: string | null;
  /** [MERGE] 全新会话是否已提交旅行需求表单：未提交时禁用输入（表单作为聊天门禁） */
  requirementSubmitted: boolean;
}

const initialState: ChatState = {
  conversations: [],
  currentConversationId: null,
  messages: [],
  isLoading: false,
  isStreaming: false,
  streamingMessageId: null,
  statusText: null,
  requirementSubmitted: false,
};

// ==================== 流式 DOM 基础设施 ====================

/**
 * 流式目标 DOM 注册表：MessageItem 在挂载时注册自身 content 元素，
 * thunk 据此直接向真实 DOM 追加 TextNode，绕开 React 重绘。
 */
export const streamingTargets = new Map<string, HTMLDivElement>();

/** 消息滚动容器引用：由 ChatMain 注册 */
export const scrollContainerRef: { current: HTMLDivElement | null } = {
  current: null,
};

let pendingScroll = false;
/** rAF 节流滚动到底部 */
export function requestScroll(): void {
  const container = scrollContainerRef.current;
  if (!container || pendingScroll) return;
  pendingScroll = true;
  requestAnimationFrame(() => {
    const el = scrollContainerRef.current;
    if (el) el.scrollTop = el.scrollHeight;
    pendingScroll = false;
  });
}

/** 消息 id 生成器（自增序列，保证并发下唯一） */
let msgSeq = 0;
function genId(prefix: string): string {
  msgSeq += 1;
  return `${prefix}-${Date.now()}-${msgSeq}`;
}

/**
 * 等待 React 完成目标消息渲染
 * MessageItem 注册 content 元素后 thunk 才能追加文本
 */
function waitForStreamTarget(id: string, timeoutMs = 5000): Promise<HTMLDivElement> {
  return new Promise((resolve, reject) => {
    const start = Date.now();
    const tick = () => {
      const el = streamingTargets.get(id);
      if (el) {
        resolve(el);
        return;
      }
      if (Date.now() - start > timeoutMs) {
        reject(new Error('stream target timeout'));
        return;
      }
      setTimeout(tick, 16);
    };
    tick();
  });
}

/**
 * 向目标元素追加分段文本（TextNode 增量）
 * 含换行时插入 <br> 并新建 TextNode，与原 JS 逻辑完全一致
 */
function appendChunkText(
  el: HTMLDivElement,
  text: string,
  textNodeRef: { node: Text },
): void {
  const parts = text.split('\n');
  for (let i = 0; i < parts.length; i += 1) {
    textNodeRef.node.textContent += parts[i];
    if (i < parts.length - 1) {
      el.appendChild(document.createElement('br'));
      textNodeRef.node = document.createTextNode('');
      el.appendChild(textNodeRef.node);
    }
  }
}

/** [MERGE] 流式累积结果：正文 / 错误文案 / 清单 id / 内嵌卡片载荷（流结束后统一提交回 redux） */
interface StreamAcc {
  fullText: string;
  errorText: string;
  planId?: string;
  planPending?: boolean;
  skillUi?: SkillUiFrame;
  /** [MERGE] 当前是否处于等待态（配合静默计时器做去重，避免重复派发） */
  waiting: boolean;
}

/** [MERGE] 等待动画静默阈值：流式分片间歇超过该时长即回到等待态展示波浪动画 */
const WAIT_QUIET_MS = 900;

/**
 * 统一事件处理 [MERGE]：按 knowgo_travel 事件 Schema 分发
 * assistant → 增量追加；skills / itinerary_plans → 状态提示；
 * error → 记录错误；end(code=401) → 全局登出；done（流自然结束）忽略
 */
function handleStreamEvent(
  evt: StreamEvent,
  el: HTMLDivElement,
  textNodeRef: { node: Text },
  acc: StreamAcc,
  dispatch: AppDispatch,
  msgId: string,
): void {
  switch (evt.role) {
    case 'assistant': {
      const chunk = typeof evt.content === 'string' ? evt.content : '';
      if (chunk) {
        acc.fullText += chunk;
        appendChunkText(el, chunk, textNodeRef);
        requestScroll();
      }
      break;
    }
    case 'assistant_reset': {
      // [MERGE] 后端择优回退：本回合已直写的内容作废，清空 DOM 与累计文本，
      // 随后由新的 assistant 帧重新推送（避免"旁白 + 正文"两份同时在屏幕上）
      acc.fullText = '';
      el.textContent = '';
      textNodeRef.node = document.createTextNode('');
      el.appendChild(textNodeRef.node);
      break;
    }
    case 'skills': {
      // [MERGE] 技能执行状态 → 状态提示条（如「行程规划：正在为您制定行程」）
      const s = evt.content as SkillEvent | undefined;
      if (s?.title) dispatch(setStreamStatus(`${s.title}：${s.desc ?? ''}`));
      break;
    }
    case 'skill_ui': {
      // [MERGE] 结构化 UI Skill 载荷（如多段高铁选择卡片）→ 挂到本条消息上，
      // 由 MessageItem 按 skill_name 查注册表内嵌渲染；此处不做任何业务校验
      const frame = evt.content as SkillUiFrame | undefined;
      if (frame?.skill_name) acc.skillUi = frame;
      break;
    }
    case 'itinerary_plans': {
      const c = evt.content as ItineraryPlanEvent | undefined;
      if (c?.status === 'success' && c.id) {
        acc.planId = c.id;
        dispatch(setStreamStatus(null));
      } else if (c?.status === 'pending') {
        // [MERGE] pending 帧：立即标记消息"清单生成中"，流式期间即显示生成中按钮
        acc.planPending = true;
        dispatch(setMsgPlanPending(msgId));
        dispatch(setStreamStatus(c.message || '行程清单生成中...'));
      } else if (c?.status === 'error') {
        acc.planPending = false;
        dispatch(setStreamStatus(null));
        dispatch(showToast({ message: c.message || '行程清单生成失败', type: 'error' }));
      }
      break;
    }
    case 'error': {
      acc.errorText =
        typeof evt.content === 'string' && evt.content ? evt.content : '服务异常，请稍后重试。';
      break;
    }
    case 'end': {
      if (evt.code === 401) notifyUnauthorized();
      break;
    }
    default:
      break;
  }
}

/**
 * 消费 SSE 响应流 [MERGE]：逐行解析 data: 统一事件帧
 * 缓冲半行 + 流式 UTF-8 解码，避免中文多字节字符被网络分包截断
 */
async function consumeStream(
  res: Response,
  msgId: string,
  dispatch: AppDispatch,
): Promise<StreamAcc> {
  const reader = res.body?.getReader();
  if (!reader) {
    const empty: StreamAcc = {
      fullText: '',
      errorText: '连接中断，请稍后重试。',
      waiting: false,
    };
    dispatch(commitStreamMessage({ id: msgId, content: empty.errorText }));
    return empty;
  }
  const decoder = new TextDecoder();
  const el = await waitForStreamTarget(msgId);
  const textNodeRef = { node: document.createTextNode('') };
  el.appendChild(textNodeRef.node);
  const acc: StreamAcc = { fullText: '', errorText: '', planPending: false, waiting: true };
  let buffer = '';

  // [MERGE] 等待波浪动画：分片流动即退出等待态；静默超过阈值（模型思考/skill 执行）即回到等待态
  let quietTimer: ReturnType<typeof setTimeout> | null = null;
  const armQuietTimer = () => {
    if (quietTimer) clearTimeout(quietTimer);
    quietTimer = setTimeout(() => {
      quietTimer = null;
      if (acc.waiting) return;
      acc.waiting = true;
      dispatch(setMsgWaiting({ id: msgId, waiting: true }));
    }, WAIT_QUIET_MS);
  };
  const stopQuietTimer = () => {
    if (quietTimer) {
      clearTimeout(quietTimer);
      quietTimer = null;
    }
  };

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() ?? ''; // 半行留待下一包拼接

    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed.startsWith('data:')) continue;
      const data = trimmed.slice(5).trim();
      if (!data || data === '[DONE]') continue;
      try {
        const evt = JSON.parse(data) as StreamEvent;
        // [MERGE] 正文分片到达：立即退出等待态（隐藏波浪动画）并重置静默计时
        if (evt.role === 'assistant' && typeof evt.content === 'string' && evt.content) {
          if (acc.waiting) {
            acc.waiting = false;
            dispatch(setMsgWaiting({ id: msgId, waiting: false }));
          }
          armQuietTimer();
        }
        handleStreamEvent(evt, el, textNodeRef, acc, dispatch, msgId);
      } catch {
        /* 单帧解析失败不中断整条流 */
      }
    }
  }
  stopQuietTimer();

  // 流结束：先清空 DOM 直写内容——commit 后 React 以 dangerouslySetInnerHTML 渲染
  // 最终文本，手动 append 的 TextNode 不归 React 管，不清空会与 span 内容并存（双份）
  el.textContent = '';
  // 把最终文本提交回 redux（统一数据源），error 优先展示
  dispatch(
    commitStreamMessage({
      id: msgId,
      content: acc.errorText || acc.fullText,
      planId: acc.planId,
      skillUi: acc.skillUi,
    }),
  );
  return acc;
}

// ==================== 异步 thunk ====================

/** 获取全部对话列表：GET /api/v1/conversations */
export const fetchConversationsThunk = createTypedAsyncThunk(
  'chat/fetchConversations',
  async (_: void, { dispatch }) => {
    try {
      const convs = await fetchConversationsApi();
      dispatch(setConversations(convs));
    } catch (e) {
      // 401 由全局鉴权处理器统一登出并提示，此处不重复弹窗
      if (e instanceof ApiError && e.status === 401) return;
      dispatch(
        showToast({ message: e instanceof Error ? e.message : '获取行程失败', type: 'error' }),
      );
    }
  },
);

/** 新建对话：POST /api/v1/conversations */
export const createConversationThunk = createTypedAsyncThunk(
  'chat/createConversation',
  async (_: void, { dispatch }) => {
    try {
      const conv = await createConversationApi();
      dispatch(setCurrentConversationId(conv.id));
      dispatch(clearMessages());
      // 与原生逻辑一致：创建后刷新列表
      dispatch(fetchConversationsThunk());
      dispatch(showToast({ message: '新行程已创建' }));
    } catch (e) {
      dispatch(showToast({ message: '创建失败', type: 'error' }));
    }
  },
);

/** 切换对话：加载标题 + 历史消息 */
export const switchConversationThunk = createTypedAsyncThunk(
  'chat/switchConversation',
  async (id: string, { getState, dispatch }) => {
    dispatch(setCurrentConversationId(id));

    // 获取标题（失败静默，不阻塞历史加载）
    try {
      const conv = await fetchConversationApi(id);
      if (getState().chat.currentConversationId !== id) return;
      dispatch(updateConversation(conv));
    } catch {
      /* 忽略标题加载失败 */
    }

    // 获取历史消息
    try {
      const msgs = await fetchHistoryApi(id);
      if (getState().chat.currentConversationId !== id) return;
      dispatch(
        setMessages(
          msgs.map((m: HistoryMessage) => ({
            id: genId('msg'),
            role: m.role,
            // [MERGE] assistant 的 content 是压缩摘要，全文在 extra_info.original
            content:
              m.role === 'assistant'
                ? m.extra_info?.original || m.content
                : m.content,
            // [MERGE] 历史 itinerary_plans 成功帧回放 → 恢复「查看行程清单」入口
            planId:
              m.extra_info?.itinerary_plans?.status === 'success' &&
              m.extra_info.itinerary_plans.id
                ? m.extra_info.itinerary_plans.id
                : undefined,
            // [MERGE] 修复 154：历史卡片帧回放 → 刷新/切会话后卡片仍内嵌在消息里重渲染
            skillUi:
              m.extra_info?.skill_ui?.skill_name
                ? m.extra_info.skill_ui
                : undefined,
            kind: 'normal' as const,
            createdAt: Date.now(),
          })),
        ),
      );
    } catch {
      if (getState().chat.currentConversationId !== id) return;
      dispatch(clearMessages());
    }
  },
);

/**
 * 发送消息：POST /api/v1/messages（SSE 流式）[MERGE]
 * 自动建会话 → 发送锁 → 用户消息 → 加载占位 → 流式消费 / 兜底渲染 → 释放锁。
 */
export const sendMessageThunk = createTypedAsyncThunk(
  'chat/sendMessage',
  async (content: string, { getState, dispatch }) => {
    const trimmed = content.trim();
    const { auth, chat } = getState() as RootState;
    if (!trimmed || chat.isLoading) return;

    // —— 无会话时先自动创建 ——
    let conversationId = chat.currentConversationId;
    if (!conversationId) {
      await dispatch(createConversationThunk()).unwrap();
      conversationId = (getState() as RootState).chat.currentConversationId;
      if (!conversationId) return;
    }

    // —— 发送锁 + 用户消息 + 助手加载占位 ——
    dispatch(setIsLoading(true));
    dispatch(
      appendMessage({
        id: genId('msg'),
        role: 'user',
        content: trimmed,
        kind: 'normal',
        createdAt: Date.now(),
      }),
    );
    const loadingId = genId('loading');
    dispatch(
      appendMessage({
        id: loadingId,
        role: 'assistant',
        content: '',
        kind: 'loading',
        createdAt: Date.now(),
      }),
    );

    try {
      const res = await streamChatApi(conversationId, trimmed);
      const isStream =
        res.ok && res.headers.get('content-type')?.includes('event-stream');

      if (!res.ok) {
        // [MERGE] SSE 端点业务错误（如 404 会话不存在）：解析 {detail}/{msg} 后走统一错误展示
        let detail = '请求失败';
        try {
          const body = (await res.json()) as { detail?: string; msg?: string };
          detail = body.detail || body.msg || detail;
        } catch {
          /* 非 JSON 响应体 */
        }
        throw new ApiError(res.status, detail);
      }

      if (isStream) {
        dispatch(removeMessage(loadingId));
        const msgId = genId('msg');
        dispatch(startStreaming(msgId));
        await consumeStream(res, msgId, dispatch);
      } else {
        // 非流式：直接渲染返回内容（data.content || data.message || JSON.stringify(data)）
        dispatch(removeMessage(loadingId));
        try {
          const data = (await res.json()) as { content?: string; message?: string };
          dispatch(
            appendMessage({
              id: genId('msg'),
              role: 'assistant',
              content: data.content || data.message || JSON.stringify(data),
              kind: 'normal',
              createdAt: Date.now(),
            }),
          );
        } catch {
          dispatch(
            appendMessage({
              id: genId('msg'),
              role: 'assistant',
              content: '连接中断，请稍后重试。',
              kind: 'normal',
              createdAt: Date.now(),
            }),
          );
        }
      }
    } catch (e) {
      // 网络异常 / 401：移除加载占位，追加错误提示
      dispatch(removeMessage(loadingId));
      const fallback =
        e instanceof ApiError && e.status !== 0 ? e.detail : '连接中断，请稍后重试。';
      dispatch(
        appendMessage({
          id: genId('msg'),
          role: 'assistant',
          content: fallback,
          kind: 'normal',
          createdAt: Date.now(),
        }),
      );
    } finally {
      dispatch(setIsLoading(false));
      // [MERGE] 兜底复位流式态：consumeStream 异常（如流目标超时）时 commit 不会到达，
      // 若 isStreaming 一直为 true，输入框会被永久禁用
      dispatch(endStreaming());
    }
  },
);

/**
 * [MERGE] 提交聊天内嵌 UI Skill 的选择结果（多段高铁卡片）：
 * POST /api/v1/skills/train_multi_segment/submit
 *
 * 后端落库后**直接续跑后续编排**并以 SSE 下发，故这里与发送消息复用同一套事件管道：
 * 新建一条助手气泡接收住宿/餐饮等后续回复（此前只回 JSON，提交完流程就停住）。
 * 返回值 { ok, msg }：ok=false 时卡片保持展开，用户可重新选择后再次提交。
 */
export const submitTrainSkillThunk = createTypedAsyncThunk(
  'chat/submitSkill',
  async (
    args: { skillName: string; payload: unknown },
    { getState, dispatch },
  ): Promise<{ ok: boolean; msg: string }> => {
    const conversationId = getState().chat.currentConversationId;
    if (!conversationId) return { ok: false, msg: '会话不存在，请刷新后重试' };
    // [MERGE] 加发送锁：续跑期间输入框整体禁用（等待中还能输入、抢发新一轮都不合理）
    dispatch(setIsLoading(true));
    try {
      // [MERGE] 按 skill_name 路由到对应提交端点（高铁/住宿各自落库并续跑后续编排）
      const res = await submitSkillApi(args.skillName, conversationId, args.payload);
      if (!res.ok) {
        // 非流式错误（如 404 会话不存在 / 422 参数非法）：解析 {detail}/{msg} 后如实告知
        let detail = '提交失败，请重试';
        try {
          const body = (await res.json()) as { detail?: string; msg?: string };
          detail = body.detail || body.msg || detail;
        } catch {
          /* 非 JSON 响应体 */
        }
        return { ok: false, msg: detail };
      }
      const msgId = genId('msg');
      dispatch(startStreaming(msgId));
      const acc = await consumeStream(res, msgId, dispatch);
      return { ok: !acc.errorText, msg: acc.errorText };
    } catch (e) {
      return { ok: false, msg: e instanceof Error ? e.message : '提交失败，请重试' };
    } finally {
      dispatch(setIsLoading(false));
      dispatch(endStreaming()); // [MERGE] 兜底复位，防止异常路径把输入框锁死
    }
  },
);

/** 行程清单查看：GET /api/v1/itinerary-plans/{planId} → 打开弹窗 [MERGE]
 *  [MERGE] 后端已保证落库即增强后完整数据（修复 34），单次拉取即可，无轮询 */
export const fetchPlanThunk = createTypedAsyncThunk(
  'chat/fetchPlan',
  async (planId: string, { dispatch }) => {
    dispatch(setPlanLoading(true));
    try {
      const plan = await fetchItineraryPlanApi(planId);
      dispatch(showPlan(plan));
    } catch (e) {
      dispatch(
        showToast({
          message: e instanceof Error ? e.message : '行程清单加载失败',
          type: 'error',
        }),
      );
    } finally {
      dispatch(setPlanLoading(false));
    }
  },
);

// ==================== 切片 ====================

const chatSlice = createSlice({
  name: 'chat',
  initialState,
  reducers: {
    resetChat() {
      return initialState;
    },
    setConversations(state, action: PayloadAction<Conversation[]>) {
      state.conversations = action.payload;
    },
    /** 更新单个对话（标题），用于切换会话后回填 */
    updateConversation(state, action: PayloadAction<Conversation>) {
      const conv = action.payload;
      const idx = state.conversations.findIndex((c) => c.id === conv.id);
      if (idx >= 0) state.conversations[idx] = conv;
      else state.conversations.unshift(conv);
    },
    setCurrentConversationId(state, action: PayloadAction<string | null>) {
      state.currentConversationId = action.payload;
      // [MERGE] 会话切换时同步表单门禁：选中已有会话放行输入；回到全新会话重新要求填表单
      state.requirementSubmitted = action.payload !== null;
    },
    /** [MERGE] 表单提交成功：放行当前全新会话的输入框 */
    markRequirementSubmitted(state) {
      state.requirementSubmitted = true;
    },
    setMessages(state, action: PayloadAction<ChatMessage[]>) {
      state.messages = action.payload;
    },
    clearMessages(state) {
      state.messages = [];
      state.statusText = null; // [MERGE] 切换会话时清空状态提示
    },
    appendMessage(state, action: PayloadAction<ChatMessage>) {
      state.messages.push(action.payload);
    },
    removeMessage(state, action: PayloadAction<string>) {
      state.messages = state.messages.filter((m) => m.id !== action.payload);
    },
    setIsLoading(state, action: PayloadAction<boolean>) {
      state.isLoading = action.payload;
    },
    /** [MERGE] 更新流式状态提示（skills 执行步骤 / 行程清单生成中） */
    setStreamStatus(state, action: PayloadAction<string | null>) {
      state.statusText = action.payload;
    },
    /** 开始流式：追加空助手消息并记录 id */
    startStreaming(state, action: PayloadAction<string>) {
      state.isStreaming = true;
      state.streamingMessageId = action.payload;
      state.statusText = null; // [MERGE] 新一轮开始，清空上一轮状态提示
      state.messages.push({
        id: action.payload,
        role: 'assistant',
        content: '',
        kind: 'normal',
        waiting: true, // [MERGE] 等待波浪动画：首个正文分片到达前保持等待态
        createdAt: Date.now(),
      });
    },
    /** [MERGE] 模型等待态切换：气泡内波浪动画的显隐（模型思考 / 分片间歇静默时展示） */
    setMsgWaiting(state, action: PayloadAction<{ id: string; waiting: boolean }>) {
      const msg = state.messages.find((m) => m.id === action.payload.id);
      if (msg) msg.waiting = action.payload.waiting;
    },
    /** 流式结束：提交最终文本（含 planId），清理流式状态 [MERGE] */
    commitStreamMessage(
      state,
      action: PayloadAction<{
        id: string;
        content: string;
        planId?: string;
        skillUi?: SkillUiFrame;
      }>,
    ) {
      const msg = state.messages.find((m) => m.id === action.payload.id);
      if (msg) {
        msg.content = action.payload.content;
        if (action.payload.planId) {
          msg.planId = action.payload.planId;
          msg.planPending = false; // success 帧已到，"生成中"态结束
        }
        // [MERGE] 内嵌 UI Skill 载荷：流结束后随消息一起落到 redux，供 MessageItem 渲染卡片。
        // 后端每轮都会重推同技能卡片（用户刷新页面后卡片消失，靠重推自愈），
        // 故此处只保留最新一张——清掉历史消息上的同技能载荷，避免同一会话叠出多张相同卡片
        if (action.payload.skillUi) {
          msg.skillUi = action.payload.skillUi;
          const skillName = action.payload.skillUi.skill_name;
          for (const m of state.messages) {
            if (m.id !== action.payload.id && m.skillUi?.skill_name === skillName) {
              delete m.skillUi;
            }
          }
        }
      }
      state.isStreaming = false;
      state.streamingMessageId = null;
      state.statusText = null; // [MERGE] 流结束，清空状态提示
    },
    /** [MERGE] 兜底结束流式态：异常路径未走 commitStreamMessage 时复位，避免输入框被锁死 */
    endStreaming(state) {
      state.isStreaming = false;
      state.streamingMessageId = null;
      state.statusText = null;
    },
    /** [MERGE] 清单生成中标记：pending 帧到达即打标，流式期间即可显示"生成中"按钮 */
    setMsgPlanPending(state, action: PayloadAction<string>) {
      const msg = state.messages.find((m) => m.id === action.payload);
      if (msg) msg.planPending = true;
    },
    /** 从 localStorage 恢复已持久化的对话列表 */
    rehydrateChat(state, action: PayloadAction<PersistedChat>) {
      if (state.conversations.length === 0 && action.payload.conversations.length > 0) {
        state.conversations = action.payload.conversations;
        state.currentConversationId = action.payload.currentConversationId;
      }
    },
  },
});

export const {
  resetChat,
  setConversations,
  updateConversation,
  setCurrentConversationId,
  markRequirementSubmitted,
  setMessages,
  clearMessages,
  appendMessage,
  removeMessage,
  setIsLoading,
  setStreamStatus,
  startStreaming,
  setMsgWaiting,
  commitStreamMessage,
  endStreaming,
  setMsgPlanPending,
  rehydrateChat,
} = chatSlice.actions;
export default chatSlice.reducer;
