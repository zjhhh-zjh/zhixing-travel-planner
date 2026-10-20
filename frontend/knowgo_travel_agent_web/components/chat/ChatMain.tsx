'use client';

import { memo, useEffect, useMemo, useState } from 'react';
import { useSelector } from 'react-redux';
import type { RootState } from '@/store';
import { requestScroll, scrollContainerRef } from '@/store/slices/chatSlice';
import MessageItem from './MessageItem';
import WelcomeScreen from './WelcomeScreen';
import ChatInput from './ChatInput';

/**
 * 右侧主聊天面板
 * - 顶部 header：AI 图标、标题、准备出发定位标签
 * - 聊天地球仪半透明水印（fa-earth-asia，300px，opacity 0.03）
 * - 消息区滚动容器：注册到 scrollContainerRef 供 SSE 流式滚动复用
 * - 消息数量 / 流式状态变化时自动滚动到底部（rAF 节流）
 */
function ChatMain() {
  const messages = useSelector((state: RootState) => state.chat.messages);
  const conversations = useSelector((state: RootState) => state.chat.conversations);
  const currentConversationId = useSelector(
    (state: RootState) => state.chat.currentConversationId,
  );
  const isStreaming = useSelector((state: RootState) => state.chat.isStreaming);
  const streamingMessageId = useSelector(
    (state: RootState) => state.chat.streamingMessageId,
  );
  // [MERGE] 流式状态提示（skills 执行步骤 / 行程清单生成中）
  const statusText = useSelector((state: RootState) => state.chat.statusText);

  // [MERGE] 挂载标记：会话列表/当前会话 id 来自客户端持久化恢复，SSR 阶段不可知——
  // currentTitle 直接渲染会 SSR("行程助手")/客户端(真实标题)不一致触发 Hydration Error
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  // 当前会话标题：无会话时显示「行程助手」，新行程回退「新行程」
  const currentTitle = useMemo(() => {
    if (!mounted) return '行程助手';
    if (!currentConversationId) return '行程助手';
    return (
      conversations.find((c) => c.id === currentConversationId)?.title ?? '新行程'
    );
  }, [mounted, currentConversationId, conversations]);

  // 新消息 / 流式状态变化 → 自动滚动到底部
  useEffect(() => {
    requestScroll();
  }, [messages.length, isStreaming]);

  return (
    <main className="glass-card relative flex min-w-0 flex-1 flex-col overflow-hidden rounded-lg">
      {/* 地球仪半透明水印（置于内容后方） */}
      <i
        aria-hidden
        className="fa-solid fa-earth-asia pointer-events-none absolute left-1/2 top-1/2 z-0 -translate-x-1/2 -translate-y-1/2 text-[300px] text-primary opacity-[0.03]"
      />

      {/* 顶部 header */}
      <header className="z-[1] flex items-center justify-between border-b border-black/5 bg-white/90 px-8 py-5 narrow:px-[15px] narrow:py-[15px]">
        <div className="flex items-center gap-4">
          <div className="flex h-12 w-12 items-center justify-center rounded-full bg-primary text-xl text-accent shadow-icon">
            <i className="fa-solid fa-robot" />
          </div>
          <div>
            <h2 className="font-serif text-xl text-text-serif">{currentTitle}</h2>
            <p className="mt-0.5 text-xs text-text-light">AI Travel Assistant</p>
          </div>
        </div>
        {/* 准备出发定位标签 */}
        <div className="flex items-center gap-1.5 rounded-full bg-accent/10 px-3 py-1.5 text-xs font-semibold text-accent">
          <i className="fa-solid fa-location-dot" /> 准备出发
        </div>
      </header>

      {/* 消息区域 */}
      <div
        ref={(el) => {
          scrollContainerRef.current = el;
        }}
        className="thin-scroll z-[1] flex flex-1 flex-col gap-7 overflow-y-auto p-8 narrow:p-4"
      >
        {/* [MERGE] mounted 前渲染 WelcomeScreen（与 SSR 一致）：messages 来自客户端持久化恢复，
            SSR 阶段为空，直接渲染消息列表会触发 Hydration Error */}
        {!mounted || messages.length === 0 ? (
          <WelcomeScreen />
        ) : (
          messages.map((msg) => {
            // [MERGE] key 含流式态后缀：commit 时强制 MessageItem 重挂载——
            // 流式 DOM 直写的 TextNode 不归 React 管，若复用同一组件会与
            // dangerouslySetInnerHTML 渲染并存（双份）或 insertBefore 报错
            const streaming = isStreaming && msg.id === streamingMessageId;
            return (
              <MessageItem
                key={`${msg.id}-${streaming ? 's' : 'd'}`}
                message={msg}
                isStreamingTarget={streaming}
              />
            );
          })
        )}
      </div>

      {/* [MERGE] 状态提示条：skills 执行步骤 / 行程清单生成中 */}
      {statusText && (
        <div className="flex items-center gap-2 border-t border-black/5 px-6 py-2 text-[13px] text-text-light">
          <i className="fa-solid fa-circle-notch fa-spin text-primary" />
          <span>{statusText}</span>
        </div>
      )}

      {/* 输入区 */}
      <ChatInput />
    </main>
  );
}

export default memo(ChatMain);
