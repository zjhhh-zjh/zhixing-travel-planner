'use client';

import { memo, useEffect, useRef } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import type { AppDispatch, RootState } from '@/store';
import { hideToast } from '@/store/slices/uiSlice';

/**
 * Toast 全局轻提示
 * - 成功 / 错误两种样式（错误为红色底）
 * - 3 秒自动滑出消失，过渡缓动曲线与原版一致
 * - 独立订阅 ui slice，仅 Toast 自身重渲染
 */
function Toast() {
  const dispatch = useDispatch<AppDispatch>();
  const toast = useSelector((state: RootState) => state.ui.toast);
  const hideTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (toast.visible) {
      if (hideTimer.current) clearTimeout(hideTimer.current);
      // 3 秒后自动消失（原版逻辑）
      hideTimer.current = setTimeout(() => dispatch(hideToast()), 3000);
    }
    return () => {
      if (hideTimer.current) clearTimeout(hideTimer.current);
    };
  }, [toast.visible, dispatch]);

  return (
    <div
      className={`fixed left-1/2 top-[30px] z-[1000] flex items-center gap-2 rounded-[50px] px-6 py-3 text-sm text-white shadow-toast transition-transform duration-[400ms] ease-[cubic-bezier(0.175,0.885,0.32,1.275)] ${
        toast.visible
          ? '-translate-x-1/2 translate-y-0'
          : '-translate-x-1/2 -translate-y-[100px]'
      } ${toast.type === 'error' ? 'bg-[#c0392b]' : 'bg-text-main'}`}
      role="status"
      aria-live="polite"
    >
      <i className="fa-solid fa-info-circle" />
      <span>{toast.message}</span>
    </div>
  );
}

export default memo(Toast);
