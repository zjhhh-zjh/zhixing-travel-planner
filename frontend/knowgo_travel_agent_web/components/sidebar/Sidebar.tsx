'use client';

import { memo, useCallback, useEffect, useRef, useState } from 'react';
import { FixedSizeList, type ListChildComponentProps } from 'react-window';
import { useDispatch, useSelector } from 'react-redux';
import type { AppDispatch, RootState } from '@/store';
import {
  createConversationThunk,
  switchConversationThunk,
} from '@/store/slices/chatSlice';
import { logoutThunk } from '@/store/slices/authSlice';
import { showToast } from '@/store/slices/uiSlice';
import type { Conversation } from '@/lib/types';
import ConversationItem from './ConversationItem';

/** 虚拟滚动行高度：原卡片 padding(14×2) + 标题 20 + 间距 4 + 时间 16 + 行间留白 ≈ 76 */
const ROW_HEIGHT = 76;

/** 虚拟列表行数据 */
interface RowData {
  conversations: Conversation[];
  activeId: string | null;
  onSelect: (id: string) => void;
}

/** 虚拟列表行：只渲染可视区域内的对话，大量对话不卡顿 */
function ConversationRow({ index, style, data }: ListChildComponentProps<RowData>) {
  const conv = data.conversations[index];
  return (
    <div style={style} className="px-1">
      <ConversationItem
        conversation={conv}
        active={conv.id === data.activeId}
        onSelect={data.onSelect}
      />
    </div>
  );
}

/**
 * 左侧护照风格侧边栏
 * - 品牌 logo、用户头像（用户名首字母）、用户名展示
 * - 新建行程按钮、历史对话列表（react-window 虚拟滚动）
 * - 登出按钮：清空 redux 状态 + 弹出登录弹窗
 * - 900px 以下：横向收起，仅保留新建对话图标按钮
 */
function Sidebar() {
  const dispatch = useDispatch<AppDispatch>();
  const user = useSelector((state: RootState) => state.auth.user);
  const conversations = useSelector((state: RootState) => state.chat.conversations);
  const currentConversationId = useSelector(
    (state: RootState) => state.chat.currentConversationId,
  );

  // —— 虚拟列表容器高度测量（ResizeObserver，窗口 resize 节流感知） ——
  const listAreaRef = useRef<HTMLDivElement>(null);
  const [listHeight, setListHeight] = useState(0);

  useEffect(() => {
    const el = listAreaRef.current;
    if (!el) return;
    const update = () => setListHeight(el.clientHeight);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const handleSelect = useCallback(
    (id: string) => {
      dispatch(switchConversationThunk(id));
    },
    [dispatch],
  );

  const handleNewChat = useCallback(() => {
    dispatch(createConversationThunk());
  }, [dispatch]);

  const handleLogout = useCallback(() => {
    dispatch(logoutThunk());
    dispatch(showToast({ message: '已退出旅程' }));
  }, [dispatch]);

  // [MERGE] 挂载标记：user 来自客户端持久化恢复，SSR 阶段恒为默认值——
  // 直接渲染会导致头像文本 SSR("U")/客户端(真实首字母)不一致触发 Hydration Error
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  // 用户名首字母（对应原 updateUserInfo 逻辑）
  const avatarText = mounted ? (user?.username || user?.email || 'U')[0].toUpperCase() : 'U';

  const rowData: RowData = {
    conversations,
    activeId: currentConversationId,
    onSelect: handleSelect,
  };

  return (
    <aside className="glass-card flex w-[280px] shrink-0 flex-col rounded-lg narrow:h-auto narrow:w-full narrow:flex-row narrow:items-center narrow:p-2.5">
      {/* 头部：logo + 用户护照卡 */}
      <div className="border-b border-dashed border-black/10 p-8 pb-5 narrow:flex-1 narrow:border-none narrow:p-0">
        <div className="flex items-center gap-2 font-serif text-2xl font-bold tracking-wider text-primary">
          <i className="fa-solid fa-mountain-sun" /> 知行
        </div>

        {/* 用户护照：移动端隐藏 */}
        <div className="mt-5 flex items-center gap-3 rounded-[8px] border border-[#e0e6e8] bg-[#f0f4f5] p-3 narrow:hidden">
          <div className="flex h-10 w-10 items-center justify-center rounded-full border-2 border-accent bg-primary text-white">
            {avatarText}
          </div>
          <div>
            <h4 className="text-sm text-primary">{mounted ? user?.username || '旅行者' : '旅行者'}</h4>
            <span className="text-[11px] uppercase text-text-light">PASSPORT</span>
          </div>
        </div>
      </div>

      {/* 新建行程按钮：移动端仅保留图标 */}
      <button
        onClick={handleNewChat}
        className="mx-6 my-5 flex items-center justify-center gap-2.5 rounded-sm border border-primary bg-transparent p-3 font-semibold text-primary transition-all duration-300 hover:bg-primary hover:text-white narrow:m-0 narrow:w-10 narrow:shrink-0 narrow:border-none narrow:p-2"
        title="规划新行程"
      >
        <i className="fa-solid fa-route" />
        <span className="narrow:hidden">规划新行程</span>
      </button>

      {/* 对话列表：移动端隐藏 */}
      <div className="flex min-h-0 flex-1 flex-col px-4 narrow:hidden">
        <div className="px-2 pb-2 text-[11px] uppercase tracking-wider text-text-light">
          Travel Logs
        </div>
        <div ref={listAreaRef} className="min-h-0 flex-1">
          {/* [MERGE] mounted 前强制渲染空态（与 SSR 一致）：conversations 来自客户端恢复、
              listHeight 来自客户端测量，SSR 均无法预知，直接渲染会触发 Hydration Error */}
          {!mounted || conversations.length === 0 ? (
            <div className="p-5 text-center text-xs text-text-light">
              暂无行程记录
            </div>
          ) : (
            <FixedSizeList
              height={listHeight}
              width="100%"
              itemCount={conversations.length}
              itemSize={ROW_HEIGHT}
              itemData={rowData}
            >
              {ConversationRow}
            </FixedSizeList>
          )}
        </div>
      </div>

      {/* 登出按钮：移动端隐藏 */}
      <button
        onClick={handleLogout}
        className="mx-5 mb-5 mt-auto flex cursor-pointer items-center gap-2 border-none bg-none p-2.5 text-[13px] text-text-light transition-colors hover:text-[#d63031] narrow:hidden"
      >
        <i className="fa-solid fa-power-off" /> 结束本次旅程
      </button>
    </aside>
  );
}

export default memo(Sidebar);
