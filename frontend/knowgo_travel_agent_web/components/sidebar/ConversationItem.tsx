'use client';

import { memo, useCallback } from 'react';
import type { Conversation } from '@/lib/types';
import { cx, formatTime } from '@/lib/utils';

interface ConversationItemProps {
  conversation: Conversation;
  /** 是否为当前激活会话 */
  active: boolean;
  onSelect: (id: string) => void;
}

/**
 * 单条对话 Item（行程卡片）
 * - hover 变白、激活态金色左侧标线 + 轻阴影
 * - 展示行程标题 + 更新时间（M月D日）
 */
function ConversationItem({ conversation, active, onSelect }: ConversationItemProps) {
  const handleClick = useCallback(() => {
    onSelect(conversation.id);
  }, [conversation.id, onSelect]);

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={handleClick}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          handleClick();
        }
      }}
      className={cx(
        'cursor-pointer rounded-[6px] bg-white/50 p-3.5 transition-all duration-200 hover:bg-white',
        'border-l-[3px] border-l-transparent',
        active &&
          'border-l-accent bg-white shadow-[0_2px_8px_rgba(0,0,0,0.05)]',
      )}
    >
      {/* 行程标题 */}
      <div className="mb-1 truncate text-sm font-medium text-text-main">
        <i className="fa-solid fa-map-pin mr-1 text-[10px] text-accent" />
        {conversation.title || '未知行程'}
      </div>
      {/* 更新时间 */}
      <div className="flex items-center gap-1 text-[11px] text-text-light">
        <i className="fa-regular fa-clock text-[10px]" />
        {formatTime(conversation.updated_at || conversation.created_at)}
      </div>
    </div>
  );
}

export default memo(ConversationItem);
