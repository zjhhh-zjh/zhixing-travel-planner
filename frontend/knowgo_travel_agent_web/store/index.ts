/**
 * Redux 根仓库配置
 * - 使用轻量自定义持久化层替代 redux-persist，避免 Turbopack chunking 问题
 * - 通过 store.subscribe 将 token 同步到请求层、auth/chat 状态写入 localStorage
 * - 注册 401 全局鉴权处理：自动登出 + Toast 提示
 */
import { combineReducers, configureStore } from '@reduxjs/toolkit';
import authReducer, {
  logoutThunk,
  rehydrateAuth,
} from './slices/authSlice';
import chatReducer, { rehydrateChat } from './slices/chatSlice';
import uiReducer, { showToast } from './slices/uiSlice';
import { setToken, setUnauthorizedHandler } from '@/lib/api';
import {
  loadPersistedAuth,
  loadPersistedChat,
  persistAuth,
  persistChat,
} from '@/lib/persist';

const rootReducer = combineReducers({
  auth: authReducer,
  chat: chatReducer,
  ui: uiReducer,
});

export const store = configureStore({
  reducer: rootReducer,
  devTools: process.env.NODE_ENV !== 'production',
  middleware: (getDefaultMiddleware) =>
    getDefaultMiddleware({
      serializableCheck: {
        ignoredActions: ['persist/PERSIST', 'persist/REHYDRATE'],
      },
    }),
});

// ==================== 持久化重水合（仅客户端） ====================

const isClient = typeof window !== 'undefined';

if (isClient) {
  const auth = loadPersistedAuth();
  if (auth?.token) {
    store.dispatch(rehydrateAuth(auth));
  }
  const chat = loadPersistedChat();
  if (chat?.conversations.length) {
    store.dispatch(rehydrateChat(chat));
  }
}

// ==================== 双向同步 ====================

// token 与请求层双向同步；auth/chat 自动持久化
store.subscribe(() => {
  const state = store.getState();
  setToken(state.auth.token);

  if (isClient) {
    persistAuth({ token: state.auth.token, user: state.auth.user });
    persistChat({
      conversations: state.chat.conversations,
      currentConversationId: state.chat.currentConversationId,
    });
  }
});

// 401 全局鉴权失效处理：自动登出 + 全局 Toast
setUnauthorizedHandler(() => {
  store.dispatch(logoutThunk());
  store.dispatch(showToast({ message: '登录已过期，请重新登录', type: 'error' }));
});

export type RootState = ReturnType<typeof rootReducer>;
export type AppDispatch = typeof store.dispatch;
