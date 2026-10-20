/**
 * 轻量级自定义持久化层
 * 替代 redux-persist，避免其 lib/storage 在 Turbopack 中的 chunking 问题。
 *
 * 用法：
 * - 在 store 创建后从 store.subscribe 中调用 persistXxx() 自动写入
 * - 在客户端模块加载时调用 loadXxx() 读取并 dispatch rehydrate action
 * - logoutThunk 中调用 clearPersistedState() 清空
 */
import type { Conversation, User } from './types';

const isClient = typeof window !== 'undefined';

const AUTH_KEY = 'zhixing:auth';
const CHAT_KEY = 'zhixing:chat';

export interface PersistedAuth {
  token: string;
  user: User | null;
}

export interface PersistedChat {
  conversations: Conversation[];
  currentConversationId: string | null;
}

/** 从 localStorage 加载已持久化的 auth 快照 */
export function loadPersistedAuth(): PersistedAuth | null {
  if (!isClient) return null;
  try {
    const raw = localStorage.getItem(AUTH_KEY);
    return raw ? (JSON.parse(raw) as PersistedAuth) : null;
  } catch {
    return null;
  }
}

/** 从 localStorage 加载已持久化的 chat 快照 */
export function loadPersistedChat(): PersistedChat | null {
  if (!isClient) return null;
  try {
    const raw = localStorage.getItem(CHAT_KEY);
    return raw ? (JSON.parse(raw) as PersistedChat) : null;
  } catch {
    return null;
  }
}

/** 持久化 auth 状态到 localStorage */
export function persistAuth(data: PersistedAuth): void {
  if (!isClient) return;
  try {
    localStorage.setItem(AUTH_KEY, JSON.stringify(data));
  } catch {
    /* 忽略存储满等异常 */
  }
}

/** 持久化 chat 状态到 localStorage */
export function persistChat(data: PersistedChat): void {
  if (!isClient) return;
  try {
    localStorage.setItem(CHAT_KEY, JSON.stringify(data));
  } catch {
    /* 忽略存储满等异常 */
  }
}

/** 登出时清空全部持久化数据 */
export function clearPersistedState(): void {
  if (!isClient) return;
  try {
    localStorage.removeItem(AUTH_KEY);
    localStorage.removeItem(CHAT_KEY);
  } catch {
    /* 忽略 */
  }
}
