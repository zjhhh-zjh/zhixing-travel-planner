/**
 * UI 全局状态切片：Toast 轻提示 + 行程清单弹窗 [MERGE]
 * Toast / PlanModal 为独立 memo 组件，只订阅本 slice，避免全局级联重渲染。
 */
import { createSlice, type PayloadAction } from '@reduxjs/toolkit';
import type { TravelItinerary } from '@/lib/types';

export type ToastType = 'success' | 'error';

export interface ToastStateItem {
  message: string;
  type: ToastType;
  visible: boolean;
}

/** 行程清单弹窗状态 [MERGE] */
export interface PlanModalState {
  visible: boolean;
  loading: boolean;
  plan: TravelItinerary | null;
}

export interface UiState {
  toast: ToastStateItem;
  planModal: PlanModalState;
}

const initialState: UiState = {
  toast: { message: '', type: 'success', visible: false },
  planModal: { visible: false, loading: false, plan: null },
};

const uiSlice = createSlice({
  name: 'ui',
  initialState,
  reducers: {
    /** 展示 Toast（3 秒后由 Toast 组件自动隐藏） */
    showToast(
      state,
      action: PayloadAction<{ message: string; type?: ToastType }>,
    ) {
      state.toast = {
        message: action.payload.message,
        type: action.payload.type ?? 'success',
        visible: true,
      };
    },
    hideToast(state) {
      state.toast.visible = false;
    },
    // ==================== 行程清单弹窗 [MERGE] ====================
    showPlan(state, action: PayloadAction<TravelItinerary>) {
      state.planModal = { visible: true, loading: false, plan: action.payload };
    },
    setPlanLoading(state, action: PayloadAction<boolean>) {
      state.planModal.loading = action.payload;
    },
    hidePlan(state) {
      state.planModal.visible = false;
    },
  },
});

export const { showToast, hideToast, showPlan, setPlanLoading, hidePlan } = uiSlice.actions;
export default uiSlice.reducer;
