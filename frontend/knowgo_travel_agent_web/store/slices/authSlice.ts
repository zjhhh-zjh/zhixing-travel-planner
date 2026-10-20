/**
 * 用户鉴权切片：登录 / 注册 / 登出
 * - 持久化白名单：仅 token 与 user 两个字段
 * - 登出时同步清空持久化存储（对应原 logout() 中 localStorage.removeItem）
 */
import { createAsyncThunk, createSlice, type PayloadAction } from '@reduxjs/toolkit';
import { loginApi, registerApi, setToken } from '@/lib/api';
import { clearPersistedState, type PersistedAuth } from '@/lib/persist';
import type { User } from '@/lib/types';
import { resetChat } from './chatSlice';

export interface AuthState {
  token: string;
  user: User | null;
  /** 提交中状态：用于登录/注册按钮加载锁 */
  submitting: boolean;
}

const initialState: AuthState = {
  token: '',
  user: null,
  submitting: false,
};

// ==================== 异步 thunk ====================

export const loginThunk = createAsyncThunk(
  'auth/login',
  async ({ username, password }: { username: string; password: string }) => {
    return await loginApi(username, password);
  },
);

export const registerThunk = createAsyncThunk(
  'auth/register',
  async ({ username, email, password }: { username: string; email: string; password: string }) => {
    return await registerApi(username, email, password);
  },
);

/**
 * 登出：清空 redux 状态 + 清空持久化存储 + 同步 token 到请求层
 */
export const logoutThunk = createAsyncThunk('auth/logout', async (_, { dispatch }) => {
  clearPersistedState();
  setToken('');
  dispatch(authLogout());
  dispatch(resetChat());
});

// ==================== 切片 ====================

const authSlice = createSlice({
  name: 'auth',
  initialState,
  reducers: {
    authLogout(state) {
      state.token = '';
      state.user = null;
      state.submitting = false;
    },
    /** 从 localStorage 恢复已持久化的登录态 */
    rehydrateAuth(state, action: PayloadAction<PersistedAuth>) {
      // 仅在当前未登录时应用持久化快照（避免覆盖新登录状态）
      if (!state.token && action.payload.token) {
        state.token = action.payload.token;
        state.user = action.payload.user;
      }
    },
  },
  extraReducers: (builder) => {
    builder
      // —— 登录 / 注册提交中：按钮加载锁 ——
      .addCase(loginThunk.pending, (state) => {
        state.submitting = true;
      })
      .addCase(registerThunk.pending, (state) => {
        state.submitting = true;
      })
      // —— 登录成功 ——
      .addCase(loginThunk.fulfilled, (state, action) => {
        state.token = action.payload.access_token;
        state.user = action.payload.user;
        state.submitting = false;
        setToken(action.payload.access_token);
      })
      .addCase(loginThunk.rejected, (state) => {
        state.submitting = false;
      })
      // —— 注册成功 ——
      .addCase(registerThunk.fulfilled, (state, action) => {
        state.token = action.payload.access_token;
        state.user = action.payload.user;
        state.submitting = false;
        setToken(action.payload.access_token);
      })
      .addCase(registerThunk.rejected, (state) => {
        state.submitting = false;
      });
  },
});

export const { authLogout, rehydrateAuth } = authSlice.actions;
export default authSlice.reducer;
