'use client';

import { memo, useEffect } from 'react';
import dynamic from 'next/dynamic';
import { useDispatch, useSelector } from 'react-redux';
import type { AppDispatch, RootState } from '@/store';
import { fetchConversationsThunk } from '@/store/slices/chatSlice';
import DecorBackground from '../components/common/DecorBackground';
import Sidebar from '../components/sidebar/Sidebar';
import ChatMain from '../components/chat/ChatMain';

// 性能优化：非首屏次要组件懒加载（next/dynamic + ssr:false），
// 拆分 chunk，减少首屏初始 JS 体积。
const Toast = dynamic(() => import('../components/common/Toast'), { ssr: false });
const AuthOverlay = dynamic(() => import('../components/auth/AuthOverlay'), {
  ssr: false,
});
const PlanModal = dynamic(() => import('../components/chat/PlanModal'), { ssr: false });

/**
 * 首页根入口：承载全局装饰层、主布局、登录弹窗与 Toast
 */
function HomePage() {
  const dispatch = useDispatch<AppDispatch>();
  const token = useSelector((state: RootState) => state.auth.token);
  const user = useSelector((state: RootState) => state.auth.user);

  useEffect(() => {
    // 对应原 DOMContentLoaded 逻辑：已登录则拉取对话列表
    if (token && user) {
      dispatch(fetchConversationsThunk());
    }
  }, [token, user, dispatch]);

  return (
    <>
      {/* 全局装饰层：等高线地图背景 + 飞行飞机尾气动画 */}
      <DecorBackground />

      {/* 主布局：左侧护照风格侧边栏 + 右侧聊天主面板 */}
      <div className="relative z-10 mx-auto flex h-screen max-w-[1600px] gap-6 p-6 narrow:flex-col narrow:gap-2.5 narrow:p-2.5">
        <Sidebar />
        <ChatMain />
      </div>

      <AuthOverlay />
      <PlanModal />
      <Toast />
    </>
  );
}

export default memo(HomePage);
